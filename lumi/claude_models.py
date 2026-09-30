"""What each Claude model accepts for thinking, effort and output length.

Anthropic changed how thinking is asked for. Older models take a fixed
budget (``thinking: {"type": "enabled", "budget_tokens": N}``). Claude Opus
4.6 and Sonnet 4.6 added adaptive thinking steered by an effort level
(``thinking: {"type": "adaptive"}`` with ``output_config: {"effort": ...}``),
and from Opus 4.7, Sonnet 5 and Fable 5 on the budget form is refused with
HTTP 400. The newest models think whether a request asks or not: Opus 5.5,
Fable and Mythos can't turn it off at all, and Sonnet 5.5 only turns off the
thinking before its answer (``thinking: {"type": "between_tools"}``).

``lumi/anthropic_api.py`` sends Lumi's thinking levels (off, low, med, high,
max; "default" sends nothing) in the shape each family accepts. The families,
from Anthropic's Messages API documentation (Thinking, Effort and the models
overview), read 2026-09-30:

- Opus 5.5: thinks without being asked and can't stop; a budget or
  "disabled" is a 400. Effort defaults to medium. Lumi's "off" is the lowest
  effort.
- Fable 5 and 5.1, Mythos 5, 5.1 and Preview: as Opus 5.5, with effort
  defaulting to high.
- Sonnet 5.5: thinks without being asked; a budget or "disabled" is a 400.
  Lumi's "off" is ``between_tools``, its lowest setting (no thinking before
  the answer), which it accepts at effort high or below.
- Opus 5 and Sonnet 5: think without being asked; "disabled" turns it off
  (on Opus 5 at effort high or below). A budget is a 400.
- Opus 4.7 and 4.8: think only when asked, adaptively; a budget is a 400.
- Opus 4.6 and Sonnet 4.6: as Opus 4.7; a budget still works there but is
  deprecated.
- Opus 4.5, Sonnet 4.5, Haiku 4.5, Sonnet 4, Sonnet 3.7, Opus 4 and 4.1:
  a budget only (at least 1,024 tokens, below max_tokens); adaptive thinking
  is a 400, and so is effort except on Opus 4.5.
- Claude 3.5 and older: no thinking.

Every family's thinking counts toward ``max_tokens``. A model id Lumi
doesn't recognize (a newer model, or a gateway's own name) gets the newest
family's shape, and a request the API refuses says so.

Opus 5.5, Fable 5.1 and Sonnet 5.5 also bind each thinking block to the
conversation before it ("preserved thinking"): replayed after the system
prompt, the tools or an earlier message changed, it is refused with a 400
for accounts created on or after 2026-08-31, so Lumi leaves such a block out
(``anthropic_api.bound_thinking``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class ClaudeModel:
    """How one Claude model takes thinking, effort and output length."""

    family: str
    # How a thinking level is sent: "adaptive" (with an effort level),
    # "budget" (a fixed thinking budget) or "none" (the model can't think).
    thinking: str
    # Whether the model thinks when a request has no thinking field.
    thinks_by_default: bool
    # How Lumi's "off" is sent: "omit" (no field: thinking is off by
    # default), "disabled", "between_tools" (Sonnet 5.5's lowest setting)
    # or "low_effort" (the model always thinks: the least it will).
    off: str
    # The effort the API uses when a request sends none; "" without effort.
    default_effort: str
    # The largest max_tokens the model accepts.
    max_output: int
    # Whether the API refuses a replayed thinking block once the conversation
    # before it changed (preserved thinking).
    binds_thinking: bool = False
    # False for an id Lumi doesn't recognize, sent the newest family's shape.
    known: bool = True

    @property
    def can_turn_off(self) -> bool:
        """Whether "off" stops thinking (not merely the lowest effort)."""
        return self.off != "low_effort"


_OPUS_5_5 = ClaudeModel("opus-5.5", "adaptive", True, "low_effort", "medium", 128_000, binds_thinking=True)
_ALWAYS_ON = ClaudeModel("fable-mythos", "adaptive", True, "low_effort", "high", 128_000)
_FABLE_5_1 = replace(_ALWAYS_ON, family="fable-5.1", binds_thinking=True)
_SONNET_5_5 = ClaudeModel("sonnet-5.5", "adaptive", True, "between_tools", "high", 128_000, binds_thinking=True)
_ON_BY_DEFAULT = ClaudeModel("opus-5-sonnet-5", "adaptive", True, "disabled", "high", 128_000)
_OPT_IN = ClaudeModel("adaptive-opt-in", "adaptive", False, "omit", "high", 128_000)
_BUDGET = ClaudeModel("budget", "budget", False, "omit", "", 64_000)
_BUDGET_32K = replace(_BUDGET, max_output=32_000)
_NO_THINKING = ClaudeModel("no-thinking", "none", False, "omit", "", 8_192)
_NO_THINKING_4K = replace(_NO_THINKING, max_output=4_096)
# The newest family's shape for an id Lumi doesn't recognize. Its default
# effort is unknown, so its output room assumes "high"; its output limit is
# the conservative one Lumi used for every model before.
NEWEST = replace(_OPUS_5_5, family="unrecognized", default_effort="high", max_output=32_000, known=False)

_KNOWN: dict[tuple[str, int, int], ClaudeModel] = {
    ("opus", 5, 5): _OPUS_5_5,
    ("opus", 5, 0): _ON_BY_DEFAULT,
    ("opus", 4, 8): _OPT_IN,
    ("opus", 4, 7): _OPT_IN,
    ("opus", 4, 6): _OPT_IN,
    ("opus", 4, 5): _BUDGET,
    ("opus", 4, 1): _BUDGET_32K,
    ("opus", 4, 0): _BUDGET_32K,
    ("opus", 3, 0): _NO_THINKING_4K,
    ("sonnet", 5, 5): _SONNET_5_5,
    ("sonnet", 5, 0): _ON_BY_DEFAULT,
    ("sonnet", 4, 6): _OPT_IN,
    ("sonnet", 4, 5): _BUDGET,
    ("sonnet", 4, 0): _BUDGET,
    ("sonnet", 3, 7): _BUDGET,
    ("sonnet", 3, 5): _NO_THINKING,
    ("sonnet", 3, 0): _NO_THINKING_4K,
    ("haiku", 4, 5): _BUDGET,
    ("haiku", 3, 5): _NO_THINKING,
    ("haiku", 3, 0): _NO_THINKING_4K,
    ("fable", 5, 1): _FABLE_5_1,
    ("fable", 5, 0): _ALWAYS_ON,
    # Mythos 5.1 is Fable 5.1 without the conversation check.
    ("mythos", 5, 1): _ALWAYS_ON,
    ("mythos", 5, 0): _ALWAYS_ON,
}

# Current ids name the tier first (claude-opus-5-5, claude-haiku-4-5-20251001);
# Claude 3 named the version first (claude-3-7-sonnet-20250219).
_TIER_FIRST = re.compile(r"claude-(opus|sonnet|haiku|fable|mythos)-(\d+)(?:-(\d{1,2}))?(?!\d)")
_VERSION_FIRST = re.compile(r"claude-(\d)(?:-(\d))?-(opus|sonnet|haiku)(?![a-z])")
_BEDROCK_PREFIX = re.compile(r"^(?:[a-z]{2,6}\.)?anthropic\.")
# A date, a Bedrock version (-v1:0, -v1), a Vertex version (@20251101),
# "-latest" and a context-size tag ("[1m]"), in any order at the end.
_VERSION_SUFFIX = re.compile(r"(?:@[0-9a-z.-]*|-v\d+(?::\d+)?|:\d+|-latest|-\d{8}|\[[^\]]*\])+$")


def normalize_model_id(model: str) -> str:
    """The Claude API's own name for a model id from any platform.

    ``us.anthropic.claude-sonnet-4-5-20250929-v1:0`` (a Bedrock inference
    profile, or its ARN), ``claude-sonnet-4-5@20250929`` (Vertex AI) and
    ``anthropic/claude-sonnet-4.5`` (a gateway's name) are all
    ``claude-sonnet-4-5``.
    """
    text = str(model or "").strip().lower()
    text = text.rsplit("/", 1)[-1]  # an ARN's resource, a gateway's "anthropic/..." name
    text = _BEDROCK_PREFIX.sub("", text)
    text = _VERSION_SUFFIX.sub("", text)
    return re.sub(r"(?<=\d)\.(?=\d)", "-", text)  # "4.5" as in "claude-sonnet-4.5"


def claude_model(model: str) -> ClaudeModel:
    """How ``model`` (any platform's id) takes thinking, effort and output length.

    An id Lumi doesn't recognize gets :data:`NEWEST`, the newest family's shape.
    """
    name = normalize_model_id(model)
    if "claude-mythos-preview" in name:
        return _ALWAYS_ON
    match = _TIER_FIRST.search(name)
    if match:
        key = (match[1], int(match[2]), int(match[3] or 0))
        return _KNOWN.get(key, NEWEST)
    match = _VERSION_FIRST.search(name)
    if match:
        key = (match[3], int(match[1]), int(match[2] or 0))
        return _KNOWN.get(key, NEWEST)
    return NEWEST
