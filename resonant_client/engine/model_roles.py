"""Explicit quality-oriented model roles for the Resonant runtime."""

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


class ModelRoleRouter:
    """Resolve explicit phase roles without opaque mid-turn model switching.

    ``backend_factory`` is injected by the GUI/runtime layer so this module
    remains backend-agnostic and can be reused by the TUI or headless engine.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
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

    def profile(self, role: ModelRole | str) -> ModelRoleProfile:
        value = role.value if isinstance(role, ModelRole) else str(role or "primary")
        return self._profiles.get(value, self._profiles[ModelRole.PRIMARY.value])

    def backend_for(self, role: ModelRole | str, fallback: Any) -> Any:
        profile = self.profile(role)
        if not self._backend_factory or not (profile.backend_type or profile.model):
            return fallback
        try:
            return self._backend_factory(profile)
        except Exception:
            return fallback

    def to_dict(self) -> dict[str, dict[str, Any]]:
        return {role: profile.to_dict() for role, profile in self._profiles.items()}
