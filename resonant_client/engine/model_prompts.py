"""Small, stable prompt contracts for the Resonant agent loop.

The runtime owns safety, permissions, context, recovery, and validation.  The
system prompt should tell the model how to work, not restate those mechanisms.
Keeping this prefix compact and byte-stable materially improves prompt-cache
reuse for local Ollama models during long tool loops.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPromptProfile:
    """A detected model family and its focused behavioral overlay."""

    family: str
    display_name: str
    guidance: str


RESONANT_CLARIFICATION_CONTRACT = """\
Clarification:
- Investigate first and resolve implementation details from repository evidence.
- Use `await_user` only for one consequential requirement or external fact that
  cannot be discovered and would be costly to assume. Never ask for permission
  to continue or for an obvious next step.
- Direct every question to the user through `await_user`, not ordinary prose.
- When choices are useful, provide 2-5 distinct options, put the recommended
  option first, and set `recommended_option` to that exact value.
- If `await_user` is unavailable, state the safest reasonable assumption and
  continue. Non-interactive workers report an unresolved choice to their parent.
"""


_COMMON_AGENT_CONTRACT = """\
You are an expert coding assistant operating inside Resonant, an agent harness.
Use tools to inspect files, execute commands, edit code, and verify results.

Work directly toward the user's requested outcome:
- Read relevant code and project instructions before editing; never invent facts.
- Make coherent, scoped changes and preserve unrelated user work.
- Use exact tool schemas. Run independent reads together when practical.
- After edits, run the most relevant available checks. Never claim an action or
  check that did not complete.
- Continue through ordinary tool failures and uncertainty. Stop only when the
  task is complete or a genuinely blocking user decision is required.
- Delegate only bounded independent work; review returned evidence yourself.
- Keep progress updates to one short line.

Final response style:
- Lead with the outcome in one or two sentences, then verification and any
  material caveat. Do not end by asking what to do next.
- No preamble, no restating the request, no filler. Prefer a short plain
  answer over a structured essay.
- Structure for scanning: headers only for distinct sections, bullets for
  parallel facts, a table when comparing enumerable things (options, costs,
  dates), bold only for the few tokens that matter.
- Give exact numbers, names, and paths. Omit caveats that would not change
  what the reader does next.
"""


_ADAPTIVE_GUIDANCE = """\
For long tasks, maintain a short checklist and reuse settled evidence instead of
rediscovering it. Use `search_tools` once when a needed specialized capability is
not in the current tool set, then call the loaded tool directly. Before finishing,
compare the actual diff and verification evidence with the complete request.
"""


_PROFILES = {
    # Family labels remain useful for telemetry. Behavior is model-neutral so
    # model switches do not change the product contract or prompt prefix shape.
    "kimi": ModelPromptProfile("kimi", "Kimi K3", _ADAPTIVE_GUIDANCE),
    "glm": ModelPromptProfile("glm", "GLM 5.x", _ADAPTIVE_GUIDANCE),
    "deepseek": ModelPromptProfile("deepseek", "DeepSeek", _ADAPTIVE_GUIDANCE),
    "generic": ModelPromptProfile("generic", "Adaptive model", _ADAPTIVE_GUIDANCE),
}


_ROLE_GUIDANCE = {
    "primary": (
        "Role: coordinate the full request and own integration and final verification."
    ),
    "subagent": (
        "Role: isolated sub-agent. Stay within the assignment, do not delegate, and "
        "return outcome, evidence, changed or inspected locations, and unresolved risk."
    ),
    "specialist": (
        "Role: specialist. Execute only the active node within its tool and output "
        "contract; return a result the orchestrator can verify."
    ),
}


def detect_model_family(model_name: str | None) -> str:
    """Classify a backend model identifier into a prompt family."""
    normalized = (model_name or "").strip().lower().replace("_", "-")
    if "kimi-k3" in normalized:
        return "kimi"
    if "deepseek" in normalized:
        return "deepseek"
    if "glm" in normalized and any(token in normalized for token in ("glm-5", "glm5")):
        return "glm"
    return "generic"


def get_model_prompt_profile(model_name: str | None) -> ModelPromptProfile:
    """Return the immutable prompt profile selected for ``model_name``."""
    return _PROFILES[detect_model_family(model_name)]


def build_model_prompt(model_name: str | None, *, role: str = "primary") -> str:
    """Render the compact invariant contract and execution-role suffix."""
    profile = get_model_prompt_profile(model_name)
    role_guidance = _ROLE_GUIDANCE.get(role, _ROLE_GUIDANCE["primary"])
    return "\n\n".join((
        _COMMON_AGENT_CONTRACT,
        RESONANT_CLARIFICATION_CONTRACT,
        profile.guidance,
        role_guidance,
    ))
