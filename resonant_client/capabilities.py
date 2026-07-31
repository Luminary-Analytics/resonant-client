"""Capability profiles for models across Resonant providers.

Profiles combine conservative family knowledge with runtime metadata from
``/api/show``. Product behavior should query these capabilities instead of
growing model-name conditionals throughout the harness.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Iterable


_TEXT_MODALITY = ("text",)


@dataclass(frozen=True)
class ModelCapabilities:
    model: str
    context_window: int
    modalities: tuple[str, ...] = _TEXT_MODALITY
    native_tools: bool | None = None
    parallel_tools: bool | None = None
    structured_output: bool | None = None
    reasoning_levels: tuple[str, ...] = ()
    prompt_caching: bool | None = None
    native_continuation: bool | None = None
    # Whether the model can usefully drive the desktop: read a screenshot
    # and act on it with coordinates. Requires vision AND reliable tool
    # calling — a text-only model handed computer_click can only invent
    # coordinates, which is worse than not offering the tool at all.
    computer_use: bool | None = None
    max_safe_concurrency: int | None = None
    source: str = "inferred"

    def supports(self, capability: str) -> bool:
        normalized = str(capability or "").strip().lower()
        aliases = {
            "vision": "image",
            "tools": "native_tools",
            "parallel_tool_calls": "parallel_tools",
            "structured_outputs": "structured_output",
            "reasoning": "reasoning_levels",
            "cache": "prompt_caching",
            "continuation": "native_continuation",
            "computer": "computer_use",
            "computer_control": "computer_use",
        }
        field = aliases.get(normalized, normalized)
        if field in {"text", "image", "audio", "video", "document"}:
            return field in self.modalities
        if field == "computer_use":
            return self.can_use_computer
        value = getattr(self, field, None)
        return bool(value)

    @property
    def can_use_computer(self) -> bool:
        """Whether this model can drive a desktop, deriving when unstated.

        An explicit True or False always wins. `None` means "nobody said", and
        the answer is then derived from vision plus native tool calling, which
        is what the capability actually requires.

        The fallback is not a nicety. Profiles are built two ways: inferred
        from the model name, and handed over literally by a provider adapter.
        Only the first was updated when this field was added, so Kimi K3 — a
        vision model with native tools, and the flagship for this feature —
        reported `computer_use=None` and was denied its own capability.
        Deriving here fixes every literal profile at once, including ones
        written later by someone who has never heard of this field.
        """
        if self.computer_use is not None:
            return bool(self.computer_use)
        return bool("image" in self.modalities and self.native_tools)

    def with_runtime_metadata(
        self,
        reported: Iterable[str] = (),
        *,
        context_window: int | None = None,
    ) -> "ModelCapabilities":
        reported_set = {str(item).strip().lower() for item in reported if str(item).strip()}
        # A non-empty Ollama capability list is authoritative for native
        # modalities; otherwise retain conservative family inference.
        modalities = {"text"} if reported_set else set(self.modalities)
        if "vision" in reported_set or "image" in reported_set:
            modalities.add("image")
        for modality in ("audio", "video", "document"):
            if modality in reported_set:
                modalities.add(modality)
        reasoning = self.reasoning_levels
        if {"thinking", "reasoning"} & reported_set and not reasoning:
            reasoning = ("low", "medium", "high")
        window = self.context_window
        if context_window is not None and int(context_window) > 0:
            window = int(context_window)
        native_tools = True if "tools" in reported_set else self.native_tools
        # Recomputed, not carried over. What the provider reports at runtime is
        # authoritative, so a model whose family inference guessed text-only
        # but which actually advertises vision + tools must gain desktop
        # control here — otherwise the runtime report is honoured for every
        # capability except this one.
        computer_use = bool("image" in modalities and native_tools)
        return replace(
            self,
            context_window=window,
            modalities=tuple(sorted(modalities, key=("text", "image", "audio", "video", "document").index)),
            native_tools=native_tools,
            computer_use=computer_use,
            structured_output=(
                True if {"structured_output", "structured-outputs"} & reported_set
                else self.structured_output
            ),
            reasoning_levels=reasoning,
            source="reported" if reported_set or context_window else self.source,
        )

    def to_dict(self) -> dict:
        return asdict(self)


def default_context_window(model: str) -> int:
    lower = str(model or "").lower()
    if lower.startswith("kimi-k3"):
        return 1_048_576
    if lower == "glm-5.2:cloud":
        return 999_424
    if lower in {"deepseek-v4-pro:cloud", "deepseek-v4-flash:cloud"}:
        return 1_048_576
    if "glm-5" in lower or ("deepseek" in lower and "pro" in lower):
        return 131_072
    return 32_768


def infer_model_capabilities(model: str) -> ModelCapabilities:
    lower = str(model or "").lower()
    base = lower.split(":", 1)[0]
    modalities = {"text"}
    if any(token in lower for token in ("vision", "-vl", "qwen2-vl", "qwen3-vl", "llava", "bakllava")):
        modalities.add("image")

    native_tools: bool | None = None
    parallel_tools: bool | None = None
    structured_output: bool | None = None
    reasoning_levels: tuple[str, ...] = ()
    concurrency: int | None = None

    if "kimi-k3" in lower:
        modalities.add("image")
        native_tools = True
        parallel_tools = True
        reasoning_levels = ("max",)
        concurrency = 4
    elif "glm-5" in lower:
        native_tools = True
        parallel_tools = True
        structured_output = not lower.endswith(":cloud")
        reasoning_levels = ("low", "medium", "high", "max")
        concurrency = 4
    elif "deepseek-v4" in lower:
        native_tools = True
        parallel_tools = True
        structured_output = not lower.endswith(":cloud")
        reasoning_levels = ("off", "high", "max")
        concurrency = 4
    elif any(token in lower for token in ("qwen3", "qwen2.5", "mistral", "devstral")):
        native_tools = True
        structured_output = True
        concurrency = 2
    elif base in {"llama2", "llama3", "codellama", "phi", "phi3", "gemma", "starcoder", "starcoder2"}:
        native_tools = False
        parallel_tools = False
        concurrency = 1

    # Driving the desktop means looking at a screenshot and acting on it with
    # coordinates, so it needs vision and dependable tool calling together.
    # Derived rather than listed per-model: any new vision model with native
    # tools gets it without a code change, which is the point of "any model
    # that supports it". A model missing either half would be reduced to
    # guessing coordinates, so it is not offered the tools at all.
    computer_use = bool("image" in modalities and native_tools)

    return ModelCapabilities(
        model=model,
        context_window=default_context_window(model),
        modalities=tuple(sorted(modalities, key=("text", "image", "audio", "video", "document").index)),
        native_tools=native_tools,
        parallel_tools=parallel_tools,
        structured_output=structured_output,
        reasoning_levels=reasoning_levels,
        max_safe_concurrency=concurrency,
        computer_use=computer_use,
    )


def extract_reported_context_length(model_info: dict) -> int | None:
    lengths: list[int] = []
    for key, value in (model_info or {}).items():
        if not str(key).lower().endswith("context_length"):
            continue
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            lengths.append(parsed)
    return max(lengths) if lengths else None
