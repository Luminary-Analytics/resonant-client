"""Explicit quality-oriented model roles for the Lumi runtime."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Callable


class ModelRole(str, Enum):
    PRIMARY = "primary"
    PLAN = "plan"
    EXPLORE = "explore"
    IMPLEMENT = "implement"
    APPLY = "apply"
    TEST = "test"
    REVIEW = "review"
    VISION = "vision"
    SUMMARIZE = "summarize"


@dataclass(slots=True)
class ModelRoleProfile:
    role: str
    backend_type: str = ""
    model: str = ""
    thinking_mode: str = ""
    max_steps: int | None = None
    permission_mode: str = ""
    system_suffix: str = ""
    require_independent_review: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_ROLE_PROFILES = {
    ModelRole.PRIMARY.value: ModelRoleProfile(role=ModelRole.PRIMARY.value),
    ModelRole.PLAN.value: ModelRoleProfile(role=ModelRole.PLAN.value, thinking_mode="max"),
    ModelRole.EXPLORE.value: ModelRoleProfile(role=ModelRole.EXPLORE.value),
    ModelRole.IMPLEMENT.value: ModelRoleProfile(
        role=ModelRole.IMPLEMENT.value,
        thinking_mode="high",
        require_independent_review=True,
    ),
    ModelRole.APPLY.value: ModelRoleProfile(role=ModelRole.APPLY.value),
    ModelRole.TEST.value: ModelRoleProfile(role=ModelRole.TEST.value),
    ModelRole.REVIEW.value: ModelRoleProfile(role=ModelRole.REVIEW.value, thinking_mode="max"),
    ModelRole.VISION.value: ModelRoleProfile(role=ModelRole.VISION.value, thinking_mode="high"),
    ModelRole.SUMMARIZE.value: ModelRoleProfile(role=ModelRole.SUMMARIZE.value),
}


MODEL_PROVIDERS = ("anthropic", "openai", "openrouter", "sonn", "kimi", "exo", "ollama", "codex",
                   "claude-code")


def parse_model_ref(text: str) -> tuple[str, str]:
    """``provider:model`` (the model may contain colons, as Ollama tags do)."""
    provider, _, model = str(text or "").strip().partition(":")
    provider = provider.strip().lower()
    if not model.strip() or not (provider in MODEL_PROVIDERS or provider.startswith("conn-")):
        raise ValueError(f"{text!r} isn't provider:model, for example anthropic:claude-sonnet-5 "
                         f"or ollama:qwen3:32b.")
    return provider, model.strip()


def parse_fallback_models(value: Any) -> list[str]:
    """Fallback models from Settings: ``provider:model`` lines, at most five."""
    lines = value.splitlines() if isinstance(value, str) else list(value or [])
    models: list[str] = []
    for line in lines:
        text = str(line).split("#", 1)[0].strip()
        if not text:
            continue
        provider, model = parse_model_ref(text)
        if f"{provider}:{model}" not in models:
            models.append(f"{provider}:{model}")
    if len(models) > 5:
        raise ValueError("List at most five fallback models.")
    return models


def parse_role_models(value: Any) -> dict[str, dict[str, str]]:
    """Role models from Settings: ``role provider:model`` lines."""
    lines = value.splitlines() if isinstance(value, str) else list(value or [])
    roles = {role.value for role in ModelRole}
    parsed: dict[str, dict[str, str]] = {}
    for line in lines:
        text = str(line).split("#", 1)[0].strip()
        if not text:
            continue
        role, _, ref = text.partition(" ")
        role = role.strip().lower()
        if role not in roles:
            raise ValueError(f"Unknown role {role!r}; use {', '.join(sorted(roles))}.")
        provider, model = parse_model_ref(ref.strip())
        parsed[role] = {"backend_type": provider, "model": model}
    return parsed


class ModelRoleRouter:
    """Resolve explicit phase roles without opaque mid-turn model switching.

    ``backend_factory`` is injected by the GUI/runtime layer so this module
    remains backend-agnostic and can be reused by the TUI or headless engine.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        workers: list[dict[str, Any]] | None = None,
        backend_factory: Callable[[ModelRoleProfile], Any] | None = None,
    ):
        raw = config or {}
        self._profiles: dict[str, ModelRoleProfile] = {}
        for role, default in DEFAULT_ROLE_PROFILES.items():
            override = raw.get(role) if isinstance(raw.get(role), dict) else {}
            merged = default.to_dict()
            merged.update(override)
            merged["role"] = role
            self._profiles[role] = ModelRoleProfile(**merged)
        self._backend_factory = backend_factory
        self._workers: dict[str, ModelRoleProfile] = {}
        for index, value in enumerate(workers or []):
            if not isinstance(value, dict):
                continue
            worker_id = str(value.get("id") or f"worker-{index + 1}").strip()
            if not worker_id:
                continue
            self._workers[worker_id] = ModelRoleProfile(
                role=str(value.get("role") or "implement"),
                backend_type=str(value.get("backend_type") or value.get("backend") or ""),
                model=str(value.get("model") or ""),
                thinking_mode=str(value.get("thinking_mode") or ""),
                max_steps=(
                    int(value["max_steps"])
                    if value.get("max_steps") not in (None, "") else None
                ),
                permission_mode=str(value.get("permission_mode") or ""),
                system_suffix=str(value.get("system_suffix") or ""),
                require_independent_review=bool(value.get("require_independent_review", False)),
            )

    def profile(self, role: ModelRole | str) -> ModelRoleProfile:
        value = role.value if isinstance(role, ModelRole) else str(role or "primary")
        return self._profiles.get(value, self._profiles[ModelRole.PRIMARY.value])

    def worker_profile(self, worker_id: str) -> ModelRoleProfile | None:
        return self._workers.get(str(worker_id or ""))

    def backend_for(
        self,
        role: ModelRole | str,
        fallback: Any,
        *,
        worker_id: str = "",
    ) -> Any:
        profile = self.worker_profile(worker_id) or self.profile(role)
        if not self._backend_factory or not (profile.backend_type or profile.model):
            return fallback
        try:
            return self._backend_factory(profile)
        except Exception:
            if worker_id:
                raise
            return fallback

    def to_dict(self) -> dict[str, dict[str, Any]]:
        return {role: profile.to_dict() for role, profile in self._profiles.items()}

    def workers_to_dict(self) -> dict[str, dict[str, Any]]:
        return {worker_id: profile.to_dict() for worker_id, profile in self._workers.items()}
