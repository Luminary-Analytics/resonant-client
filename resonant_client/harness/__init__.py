"""
Harness core package.

This package contains the reusable planner/generator/evaluator orchestration
and `.resonant-harness` state management logic. UI surfaces such as the GUI
should import from here rather than owning the harness implementation.
"""

from .state import (
    HARNESS_DIRNAME,
    EvaluatorReport,
    HarnessWorkspace,
    ProductSpec,
    ProgressState,
    SprintContract,
)
from .orchestrator import (
    HarnessCycleRun,
    HarnessCycleStatus,
    HarnessCycleStep,
    HarnessOrchestrator,
)
from .service import HarnessService

__all__ = [
    "HARNESS_DIRNAME",
    "EvaluatorReport",
    "HarnessWorkspace",
    "ProductSpec",
    "ProgressState",
    "SprintContract",
    "HarnessCycleRun",
    "HarnessCycleStatus",
    "HarnessCycleStep",
    "HarnessOrchestrator",
    "HarnessService",
]
