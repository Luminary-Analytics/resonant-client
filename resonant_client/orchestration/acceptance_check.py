"""
Acceptance-criteria validation dispatchers — v0.5.0a2.

This module turns a typed `AcceptanceCriterion` (from `gui/roadmap.py`)
into a real, executed check whose result REFLECT writes back to the
roadmap. It is the "measure twice, cut once" enforcement layer — the
runner literally invokes the checks the user signed off on during the
rigorous grill.

See `docs/long-running-agents-phase-2.md` §11 for the full design.

────────────────────────────────────────────────────────────────────
Architecture: a thin TOOL CHEST, not a parallel agent runtime
────────────────────────────────────────────────────────────────────

REFLECT runs as a normal specialist Session via the existing
plan-graph runner. Inside that Session it can call any tool in its
allowlist (browser_navigate, browser_click, browser_screenshot,
bash, etc.). For [chrome] criteria, REFLECT drives the browser
AGENTICALLY — the model decides what to click, what to assert, what
JS to run — using the engine tools that already exist.

This module exists for the DETERMINISTIC parts:

  * [bash] — extracting the command from criterion prose, running
    it via subprocess, structuring the result. No model needed.
  * [vision] — sending a screenshot + question to an Ollama vision
    model, parsing yes/no out of the response. No agentic loop
    needed; one HTTP call.
  * [manual] — explicit no-op that returns a "skipped" result so
    REFLECT records it in the handoff but doesn't touch convergence.

For [chrome], `dispatch()` returns `CheckResult(skipped=True,
evidence="delegate_to_model")` — REFLECT then drives the browser
itself via the engine tools, captures evidence (screenshot path,
DOM state assertion result), and calls `update_criterion()` on the
roadmap directly. The deterministic helpers below (e.g.
`evaluate_browser_assertion`) are available to REFLECT for the
common case of "navigate, click, assert via getComputedStyle".

────────────────────────────────────────────────────────────────────
Why dependency-inject the runners
────────────────────────────────────────────────────────────────────

`BashRunner`, `VisionRunner`, and `BrowserAssertion` are dataclasses
because tests need to stub them. Production wires real subprocess +
httpx; unit tests inject canned responses. Same pattern as
v0.4.6's per-tier context budget — the tests don't need a live
Ollama or a live shell to verify the dispatch logic.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

from ..gui.roadmap import AcceptanceCriterion

logger = logging.getLogger(__name__)


# ── Result types ──────────────────────────────────────────────────────


@dataclass
class CheckResult:
    """Outcome of running one acceptance criterion's validation.

    Three terminal states:
      * passed=True  — the criterion's condition holds. REFLECT will
                       call `update_criterion(passed=True, evidence=...)`
                       which marks the roadmap checkbox `[x]`.
      * passed=False — the criterion's condition does NOT hold (the
                       check ran but the assertion failed). REFLECT
                       writes `[FAIL]` prefix to the roadmap so the
                       user sees what's still pending.
      * skipped=True — `[manual]` criterion or a [chrome] criterion
                       that needs agentic execution. NOT a failure.
                       Excluded from convergence iff the criterion's
                       type is `[manual]`; for `[chrome]`-skipped,
                       REFLECT picks up and runs it agentically.

    The `error` field is for the check ITSELF failing to run (vision
    model unreachable, subprocess timeout, etc.) — distinct from the
    check running and returning a "fail" assertion result. The
    distinction matters: error = retry might help; passed=False =
    actual gap in the work.
    """
    passed: bool = False
    evidence: str = ""
    error: str = ""
    skipped: bool = False

    @classmethod
    def skip_manual(cls) -> "CheckResult":
        return cls(passed=False, skipped=True,
                   evidence="manual: excluded from convergence")

    @classmethod
    def delegate_to_model(cls, reason: str = "") -> "CheckResult":
        # [chrome] criteria — REFLECT must drive the browser
        # agentically. The dispatcher returns this sentinel so
        # REFLECT knows the deterministic path didn't apply.
        return cls(passed=False, skipped=True,
                   evidence=f"delegate_to_model{': ' + reason if reason else ''}")

    @classmethod
    def errored(cls, message: str) -> "CheckResult":
        return cls(passed=False, error=message)


# ── Bash runner ───────────────────────────────────────────────────────


@dataclass
class BashRunner:
    """Wraps subprocess for [bash] criteria. Stub for tests by
    constructing with a custom `_run` callback.

    v0.5.1a4 — on Windows, `shell=True` defaults to `cmd.exe` which
    doesn't have POSIX tools like `wc`, `find`, `grep`. Real specs
    routinely use these (the v0.5.0 GA smoke's `wc -l <
    wordcount.py` failed for exactly this reason). When `bash` is
    on PATH (Git Bash ships with most Windows Python installs) we
    use it; otherwise we fall back to the platform default.
    """
    timeout_seconds: float = 60.0
    cwd: Optional[str] = None
    # Override hook for tests. Production leaves None; .run() uses
    # subprocess. Tests inject `_run=lambda cmd, **kw: (rc, out, err)`.
    _run: Optional[Callable[..., tuple[int, str, str]]] = None
    # Override hook for tests that want to pin shell-detection
    # behavior. Production leaves None; .run() uses _detect_bash().
    _bash_path: Optional[str] = None

    def run(self, command: str) -> tuple[int, str, str]:
        """Returns (returncode, stdout, stderr). Never raises on
        non-zero exit; the caller decides what failure means."""
        if self._run is not None:
            return self._run(command, cwd=self.cwd, timeout=self.timeout_seconds)
        try:
            # v0.5.1a4 — prefer bash on Windows so POSIX commands
            # (`wc`, `find`, `grep` etc.) work portably. On macOS /
            # Linux the default shell IS bash-compatible so this is
            # a no-op there.
            bash_path = self._bash_path
            if bash_path is None:
                bash_path = _detect_bash()

            if bash_path:
                # Run `bash -c <command>`. The criterion's command
                # string flows through bash's own parser, which
                # handles redirects (`<`), pipes, and quoting the
                # same way on Windows-with-Git-Bash and Linux/macOS.
                proc = subprocess.run(
                    [bash_path, "-c", command],
                    cwd=self.cwd,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                    check=False,
                )
            else:
                # Platform default shell. On Linux/macOS this is bash
                # / zsh anyway; on Windows it's cmd.exe with the
                # known POSIX-tool gap.
                proc = subprocess.run(
                    command,
                    shell=True,
                    cwd=self.cwd,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                    check=False,
                )
            return proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            return 124, exc.stdout or "", f"timeout after {self.timeout_seconds}s"
        except Exception as exc:
            return 127, "", f"subprocess error: {exc}"


# Module-level cache for the bash detection — `shutil.which` is
# cheap but we'd call it on every criterion otherwise.
_BASH_PATH_CACHE: Optional[str] = None
_BASH_PATH_CACHED = False


def _detect_bash() -> Optional[str]:
    """Return the absolute path to `bash` if it's on PATH, else None.

    Cached per-process. On Windows this typically finds Git Bash's
    `C:\\Program Files\\Git\\bin\\bash.exe`. On macOS / Linux it
    finds the system bash at `/bin/bash` or `/usr/bin/bash`.

    None means: fall back to the platform default shell — which on
    Windows is `cmd.exe` (limited, no POSIX tools) and elsewhere is
    typically a bash-compatible shell.
    """
    global _BASH_PATH_CACHE, _BASH_PATH_CACHED
    if _BASH_PATH_CACHED:
        return _BASH_PATH_CACHE
    import shutil
    _BASH_PATH_CACHE = shutil.which("bash")
    _BASH_PATH_CACHED = True
    return _BASH_PATH_CACHE


def _reset_bash_detection_cache() -> None:
    """Test helper — forget any cached bash detection. Tests that
    swap PATH or stub `shutil.which` should call this between
    runs."""
    global _BASH_PATH_CACHE, _BASH_PATH_CACHED
    _BASH_PATH_CACHE = None
    _BASH_PATH_CACHED = False


# ── Bash command extraction ──────────────────────────────────────────


# Patterns the rigorous grill is told to produce:
#   "`npm run build` exits 0"
#   "`pytest -q` exits 0 with no failures"
#   "exactly N files: `ls src | wc -l == N`"
#
# We extract the FIRST single-backtick-quoted command from the prose.
# If that command itself contains a comparison ("== N"), we treat
# the part before the operator as the command and the rest as the
# expected output assertion.
_BACKTICK_CMD_RE = re.compile(r"`([^`]+)`")
_EQUALS_ASSERTION_RE = re.compile(r"^(.+?)\s*==\s*(.+?)$")
# v0.5.1a4 — Tightened the `<` and `>` patterns to require an
# INTEGER on the right side. Without this, shell input redirects
# (e.g. `wc -l < file.py`) were mis-matched as the assertion
# operator, causing the parser to split the command and treat the
# filename as a numeric comparand. Found in v0.5.1 final smoke:
# `wc -l < wordcount.py` was being parsed as
# `command="wc -l", mode="output_lt", expected="wordcount.py"`
# → int("wordcount.py") raised → criterion silently failed even
# though the actual command would have produced 108 > 5 = pass.
#
# Legit Form-A usage (`grep -c FIXME src/ < 3`, `git log | wc -l > 5`)
# always has an integer comparand, so requiring \d+ is safe.
_LT_ASSERTION_RE = re.compile(r"^(.+?)\s*<\s*(\d+)\s*$")
_GT_ASSERTION_RE = re.compile(r"^(.+?)\s*>\s*(\d+)\s*$")

# Phrases that indicate "non-zero exit code is the pass condition"
# — i.e. the command should FAIL for the criterion to pass.
# Example: `! grep -rn ": any" src/` (grep returns 1 when no match;
# the leading `!` inverts to pass when grep finds nothing).
# We detect this so the criterion text "no `any` types" maps to
# "the bash command we care about returns 1, not 0."
_NEGATION_PREFIXES = ("! ", "not ", "no ")


@dataclass
class BashAssertion:
    """Parsed structure of a [bash] criterion. The `command` is what
    we actually run; `expected` is what we compare the output to.
    Three modes:
      * mode="exit_zero"  — pass iff returncode == 0
      * mode="exit_nonzero" — pass iff returncode != 0 (negated check)
      * mode="output_eq"  — pass iff stdout-trimmed == expected_value
      * mode="output_lt"  — pass iff int(stdout-trimmed) < int(expected)
      * mode="output_gt"  — pass iff int(stdout-trimmed) > int(expected)
    """
    command: str
    mode: str = "exit_zero"
    expected_value: str = ""

    def evaluate(self, returncode: int, stdout: str, stderr: str) -> tuple[bool, str]:
        """Return (passed, evidence_string)."""
        out_trim = stdout.strip()
        if self.mode == "exit_zero":
            passed = returncode == 0
            evidence = (
                f"exit={returncode}; stdout[:200]={out_trim[:200]!r}"
                if passed else
                f"exit={returncode}; stderr[:200]={stderr.strip()[:200]!r}"
            )
            return passed, evidence
        if self.mode == "exit_nonzero":
            passed = returncode != 0
            evidence = f"exit={returncode} (non-zero expected); stdout[:200]={out_trim[:200]!r}"
            return passed, evidence
        if self.mode == "output_eq":
            passed = out_trim == self.expected_value.strip()
            return passed, f"stdout={out_trim!r}; expected={self.expected_value!r}"
        if self.mode in ("output_lt", "output_gt"):
            try:
                actual = int(out_trim)
                expected = int(self.expected_value)
            except ValueError:
                return False, f"non-integer comparison: stdout={out_trim!r}"
            if self.mode == "output_lt":
                return actual < expected, f"stdout_int={actual} < expected={expected}"
            return actual > expected, f"stdout_int={actual} > expected={expected}"
        return False, f"unknown assertion mode {self.mode!r}"


def parse_bash_assertion(criterion_text: str) -> Optional[BashAssertion]:
    """Extract a `BashAssertion` from a criterion's prose.

    The grill prompt is told to produce one of these shapes:
      * `` `<command>` `` — pass iff exit 0 (most common)
      * `` ! `<command>` `` — pass iff exit != 0 (negation; `!`
        prefix INSIDE the backticks)
      * `` `<command>` exits 0 `` / `` `<command>` exit 0 `` —
        explicit exit-zero (default semantics; verbose form)
      * `` `<command>` exits N `` / `` `<command>` exit N `` —
        non-zero exit for any N != 0 → exit_nonzero
      * `` `<command>` output == <value> `` — pass iff stdout
        equals (suffix prose AFTER the backtick); also accepts
        `` `<command> == <value>` `` with the operator inside
      * `` `<command>` output > N `` / `` `<command>` output < N ``
        — numeric comparisons; also accepts inside backticks

    Picks the LONGEST single-backtick block in the text. Real
    criteria often have multiple backtick blocks: prose mentions
    type identifiers like `any` or `null`, then the actual
    executable command is in a longer block. Length is a reliable
    heuristic — commands have shell tokens (`-flag`, ` `, `|`,
    `&&`, etc.) and run several characters; type identifiers are
    typically one word.

    Returns None if no parseable command is found — caller should
    treat that as `CheckResult.errored("no command in criterion
    text")`.
    """
    matches = list(_BACKTICK_CMD_RE.finditer(criterion_text))
    if not matches:
        return None
    # Pick the longest backtick block; ties broken by source order
    # (earlier wins, matching the original "first block" intent for
    # criteria with only one block).
    longest = max(matches, key=lambda m: len(m.group(1)))
    raw = longest.group(1).strip()

    # Negated check: `! <command>` (literal prefix). Wins over any
    # trailing prose: if the user writes `` `! grep ...` exits 0 ``,
    # the `!` is a parser-level inversion hint — we run `grep` (not
    # `! grep`) and the assertion is "grep exit != 0". The trailing
    # "exits 0" reads as the FULL `! grep` pipeline's exit code from
    # a human's POV, which corresponds to mode=exit_nonzero on the
    # bare command. The negation phrases ("not", "no") are NOT shell
    # syntax — they're hints; the grill prompt tells the model to
    # use literal `!` as the prefix when it wants exit-nonzero
    # semantics.
    if raw.startswith("! "):
        return BashAssertion(command=raw[2:].strip(), mode="exit_nonzero")

    # Form A: operator INSIDE the backticks
    # (`<cmd> < N` / `<cmd> > N`). Numeric comparisons only.
    #
    # `==` was DROPPED in v0.5.2a4. Real-world criteria contain
    # `==` inside the command text (Python `assert x == y`, bash
    # `[[ "$a" == "$b" ]]`, etc.), and Form A `_EQUALS_ASSERTION_RE`
    # would mis-match the first `==` it found, splitting the
    # command at the wrong point. Found in the v0.5.2 GA roguelite
    # smoke: a tsconfig-strict-check criterion of the form
    # ``cat tsconfig.json | python -c "...assert c[...]['strict']==True" && echo ok` output == ok``
    # was being parsed as
    # `command="cat ... assert c[...]", expected="True ... && echo ok"`
    # → command malformed → criterion silently failed.
    #
    # Form B (trailing `output == X` AFTER the backticks) covers
    # all real Form-A `==` use cases without the syntax conflict.
    # `<` and `>` stay because they're tightened to require `\d+`
    # (per v0.5.1a4) which excludes Python/shell quotes by
    # construction.
    for op_re, mode in (
        (_LT_ASSERTION_RE, "output_lt"),
        (_GT_ASSERTION_RE, "output_gt"),
    ):
        m = op_re.match(raw)
        if m:
            return BashAssertion(
                command=m.group(1).strip(),
                mode=mode,
                expected_value=m.group(2).strip(),
            )

    # Form B (v0.5.0 GA prep) — operator AFTER the backtick block,
    # in the trailing prose. Found during the first GA smoke: the
    # bootstrap-roguelite-style `` `find src -type f | wc -l` output
    # == 6 `` was being parsed as exit_zero only (the `output == 6`
    # was ignored), so the criterion silently passed when the file
    # count was wrong. The design doc's spec example uses this form
    # too — the parser was the lagging piece.
    trailing = criterion_text[longest.end():].strip()
    if trailing:
        suffix_match = _SUFFIX_ASSERTION_RE.match(trailing)
        if suffix_match:
            op = suffix_match.group(1)
            value = suffix_match.group(2).strip()
            # Strip surrounding quotes from the value (model often
            # quotes string values for clarity).
            if (len(value) >= 2 and
                value[0] == value[-1] and value[0] in ('"', "'")):
                value = value[1:-1]
            mode = {
                "==": "output_eq",
                "<": "output_lt",
                ">": "output_gt",
            }[op]
            return BashAssertion(command=raw, mode=mode, expected_value=value)
        # Form C — `` `<cmd>` exits N `` / `` `<cmd>` exit N ``.
        # Maps N=0 → exit_zero, N!=0 → exit_nonzero.
        exits_match = _SUFFIX_EXIT_RE.match(trailing)
        if exits_match:
            code = int(exits_match.group(1))
            return BashAssertion(
                command=raw,
                mode="exit_zero" if code == 0 else "exit_nonzero",
            )

    return BashAssertion(command=raw, mode="exit_zero")


# Suffix forms that come AFTER the backtick block. Optional leading
# verbose words ("output", "stdout") then the operator + value.
# Examples:
#   "output == 6"
#   "output == hello world"
#   "stdout > 100"
#   "== 6"  (no leading word)
_SUFFIX_ASSERTION_RE = re.compile(
    r"^(?:output|stdout|result)?\s*(==|<|>)\s*(.+?)\s*$",
    re.IGNORECASE,
)
# `` `<cmd>` exits 0 `` / `` `<cmd>` exit 1 ``.
_SUFFIX_EXIT_RE = re.compile(
    r"^exits?\s+(\d+)\s*$",
    re.IGNORECASE,
)


def run_bash_check(
    criterion: AcceptanceCriterion,
    *,
    runner: Optional[BashRunner] = None,
    cwd: Optional[str] = None,
) -> CheckResult:
    """Run a [bash] acceptance check. `cwd` defaults to the runner's
    cwd; pass an explicit one to override per-call (e.g. the
    project_path of the current mission).

    Idempotent: running the same criterion twice produces the same
    result (assuming the underlying world doesn't change between
    calls). REFLECT relies on this — it may run the same check more
    than once across iterations and the convergence check needs
    consistent answers.
    """
    if criterion.type != "bash":
        return CheckResult.errored(
            f"run_bash_check called with type={criterion.type!r}"
        )

    assertion = parse_bash_assertion(criterion.text)
    if assertion is None:
        return CheckResult.errored(
            f"no parseable command in criterion: {criterion.text!r}. "
            f"Expected a single-backtick-quoted command."
        )

    runner = runner or BashRunner()
    if cwd is not None:
        runner = BashRunner(
            timeout_seconds=runner.timeout_seconds,
            cwd=cwd,
            _run=runner._run,
        )

    rc, stdout, stderr = runner.run(assertion.command)
    passed, evidence = assertion.evaluate(rc, stdout, stderr)
    return CheckResult(passed=passed, evidence=evidence)


# ── Vision runner ─────────────────────────────────────────────────────


# Default vision model per the v0.5.0 design (open question #11
# resolution). Configurable via Settings → Vision model; the default
# below is what `detect_backends` checks for at mission start.
# v0.5.0a9 — switched default from qwen2.5vl:7b → qwen3-vl:8b.
# qwen3-vl is Alibaba's explicit successor (DeepStack ViT fusion,
# Interleaved-MRoPE, expanded OCR for 32 langs); same VRAM class,
# same Apache-2 license, on Ollama today, and already 2x more pulls
# than qwen2.5vl per the Ollama library. Pre-GA web research +
# Mac Studio pull validated the choice. Users can override via
# Settings → Vision → default_model.
DEFAULT_VISION_MODEL = "qwen3-vl:8b"


@dataclass
class VisionRunner:
    """Asks an Ollama vision-capable model whether an image matches
    a question. Returns (verdict, raw_response). Production wraps
    httpx; tests stub via the `_call` hook."""
    ollama_url: str = "http://10.0.0.133:11434"
    model: str = DEFAULT_VISION_MODEL
    timeout_seconds: float = 60.0
    # Override hook for tests. Receives (model, prompt, image_b64);
    # returns the raw text response from /api/chat.
    _call: Optional[Callable[..., str]] = None
    # Override hook for is_available(). Defaults to a real /api/tags
    # probe; tests stub.
    _list_models: Optional[Callable[[], list[str]]] = None

    def is_available(self) -> bool:
        """True iff the configured model is in Ollama's local model
        list. Used by `detect_backends` at mission start AND by REFLECT
        before running a [vision] check (graceful degradation)."""
        if self._list_models is not None:
            try:
                return self.model in self._list_models()
            except Exception:
                return False
        try:
            import httpx
            resp = httpx.get(
                f"{self.ollama_url}/api/tags",
                timeout=httpx.Timeout(connect=2.0, read=5.0, write=5.0, pool=5.0),
            )
            resp.raise_for_status()
            data = resp.json()
            names = {m.get("name", "") for m in data.get("models", [])}
            return self.model in names
        except Exception as exc:
            logger.debug("VisionRunner.is_available probe failed: %s", exc)
            return False

    def ask(self, image_bytes: bytes, question: str) -> tuple[bool, str]:
        """Send the image + question, parse the model's yes/no.

        Returns (verdict, raw_response). Verdict is True iff the
        response contains an unambiguous "yes" near its start. Any
        ambiguity → False (defensive: when in doubt, the criterion
        does NOT pass; user can re-run or switch models).
        """
        prompt = self._build_prompt(question)
        if self._call is not None:
            try:
                raw = self._call(self.model, prompt, image_bytes)
            except Exception as exc:
                return False, f"_call hook raised: {exc}"
        else:
            raw = self._call_ollama(prompt, image_bytes)

        verdict = self._parse_yes_no(raw)
        return verdict, raw

    def _build_prompt(self, question: str) -> str:
        """The vision model prompt is intentionally tight: one yes/no
        question, demand the answer in the first word, follow with
        a one-sentence justification. Ambiguous answers are treated
        as 'no' by `_parse_yes_no`."""
        return (
            f"Look at the image and answer this question with YES or NO "
            f"as the very first word of your reply, then give a "
            f"one-sentence justification.\n\n"
            f"Question: {question}\n\n"
            f"Answer (YES/NO + one sentence):"
        )

    @staticmethod
    def _parse_yes_no(response: str) -> bool:
        """Extract a yes/no verdict from the model's prose. The first
        non-whitespace token must start with 'YES' (case-insensitive)
        for True; anything else is False."""
        if not response:
            return False
        first_word = response.strip().split(maxsplit=1)[0] if response.strip() else ""
        return first_word.upper().startswith("YES")

    def _call_ollama(self, prompt: str, image_bytes: bytes) -> str:
        """Real /api/chat call. Lazy import of httpx so tests that
        stub `_call` don't need it."""
        import base64
        import httpx

        b64 = base64.b64encode(image_bytes).decode("ascii")
        body = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                    "images": [b64],
                }
            ],
            "stream": False,
        }
        try:
            resp = httpx.post(
                f"{self.ollama_url}/api/chat",
                json=body,
                timeout=httpx.Timeout(connect=5.0, read=self.timeout_seconds,
                                      write=10.0, pool=self.timeout_seconds),
            )
            resp.raise_for_status()
            data = resp.json()
            return str(data.get("message", {}).get("content", ""))
        except Exception as exc:
            return f"<vision model error: {exc}>"


def run_vision_check(
    criterion: AcceptanceCriterion,
    image_bytes: bytes,
    *,
    runner: Optional[VisionRunner] = None,
) -> CheckResult:
    """Run a [vision] acceptance check.

    REFLECT is responsible for capturing the screenshot (via
    `browser_screenshot` for web or `computer_screenshot` for
    desktop) and passing the bytes here. The criterion's `text` is
    the question we ask the vision model.

    Idempotent up to vision-model determinism. The model itself may
    return slightly different prose on different calls, but the
    yes/no verdict is stable for clear-cut criteria. For ambiguous
    visual states, retries can flip the verdict — this is a known
    limitation of [vision] checks vs deterministic [bash] checks.
    """
    if criterion.type != "vision":
        return CheckResult.errored(
            f"run_vision_check called with type={criterion.type!r}"
        )

    runner = runner or VisionRunner()
    if not runner.is_available():
        return CheckResult.errored(
            f"vision model {runner.model!r} not available at "
            f"{runner.ollama_url}. Pull it with `ollama pull {runner.model}` "
            f"or set a different model in Settings → Vision."
        )

    if not image_bytes:
        return CheckResult.errored("vision check called with empty image bytes")

    try:
        verdict, raw = runner.ask(image_bytes, criterion.text)
    except Exception as exc:
        return CheckResult.errored(f"vision call raised: {exc}")

    # Truncate long model responses for the evidence string.
    raw_trim = raw.strip()
    if len(raw_trim) > 300:
        raw_trim = raw_trim[:297] + "..."
    return CheckResult(
        passed=verdict,
        evidence=f"vision_model={runner.model}; verdict={'YES' if verdict else 'NO'}; "
                 f"raw={raw_trim!r}",
    )


# ── Top-level dispatcher ──────────────────────────────────────────────


@dataclass
class CheckContext:
    """Everything the dispatcher needs to run a check that's NOT in
    the criterion itself. Constructed once per REFLECT pass.

    `image_provider` is a callable REFLECT supplies that produces
    the screenshot bytes for [vision] checks — usually wraps
    `browser_screenshot` (for web missions) or `computer_screenshot`
    (for desktop). Returning None means "no screenshot yet" and
    [vision] checks return CheckResult.errored.
    """
    project_path: str = ""
    bash_runner: Optional[BashRunner] = None
    vision_runner: Optional[VisionRunner] = None
    image_provider: Optional[Callable[[], Optional[bytes]]] = None


def dispatch(
    criterion: AcceptanceCriterion,
    context: Optional[CheckContext] = None,
) -> CheckResult:
    """Route a criterion to the right deterministic check, OR signal
    that REFLECT must drive it agentically.

    Returns:
      * `[bash]` → fully deterministic; runs and returns CheckResult
      * `[chrome]` → CheckResult.delegate_to_model() — REFLECT picks
        up via the engine's browser tools
      * `[vision]` → fully deterministic IF context.image_provider
        produces bytes; errored otherwise
      * `[manual]` → CheckResult.skip_manual()
    """
    context = context or CheckContext()

    if criterion.type == "manual":
        return CheckResult.skip_manual()

    if criterion.type == "bash":
        return run_bash_check(
            criterion,
            runner=context.bash_runner,
            cwd=context.project_path or None,
        )

    if criterion.type == "chrome":
        return CheckResult.delegate_to_model(
            "chrome criteria run via REFLECT's agentic browser tool calls"
        )

    if criterion.type == "vision":
        provider = context.image_provider
        if provider is None:
            return CheckResult.errored(
                "vision check needs context.image_provider; REFLECT must "
                "capture a screenshot before dispatching"
            )
        try:
            image_bytes = provider()
        except Exception as exc:
            return CheckResult.errored(f"image_provider raised: {exc}")
        if not image_bytes:
            return CheckResult.errored("image_provider returned empty bytes")
        return run_vision_check(criterion, image_bytes, runner=context.vision_runner)

    return CheckResult.errored(f"unknown criterion type {criterion.type!r}")


# ── Convenience: format evidence for roadmap update ───────────────────


def summarize_for_roadmap(result: CheckResult) -> str:
    """Produce the `evidence` string that REFLECT writes back to the
    roadmap via `update_criterion(passed=, evidence=...)`.

    Keeps it compact — the roadmap is human-readable; the full audit
    log captures the verbose details. Format:
      pass: "PASS: <evidence>"
      fail: "FAIL: <evidence>"
      err:  "ERROR: <error message>"
      skip: "SKIP: <reason>"
    """
    if result.error:
        return f"ERROR: {result.error}"
    if result.skipped:
        return f"SKIP: {result.evidence}"
    if result.passed:
        return f"PASS: {result.evidence}"
    return f"FAIL: {result.evidence}"
