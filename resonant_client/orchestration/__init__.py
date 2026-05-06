"""
Organic AI orchestration primitives for Resonant.

Five primitives, AI-native (not borrowed from human Agile cycles):

1. Intent      - durable user goal, mutable as understanding grows
2. Plan-graph  - DAG of nodes with goal/status/confidence/dependencies
3. Specialist  - per-node agent specialization (explore/implement/verify/...)
4. Reflection  - continuous confidence signals + auto-spawned verify/repair
5. Skill       - reusable verified procedures auto-extracted from successful runs

State lives at `~/.resonant/projects/<sha1[:12]>/plans/`, never in the user's
repo. Skills live in `~/.resonant/skills/`. Plan-graph snapshots support
rollback if a silently-pruned branch turns out to be needed later.
"""

from .plan_graph import PlanGraph, PlanNode, NodeStatus, NodeSpecialization, new_node_id
from .persistence import (
    save_graph,
    load_graph,
    snapshot_graph,
    list_snapshots,
    restore_snapshot,
    purge_old_snapshots,
    plans_dir,
)
from .specialists import (
    SPECIALISTS,
    SpecialistProfile,
    get_specialist,
    assemble_system_prompt,
    filter_tools_for_specialist,
)
from .walker import (
    GraphWalker,
    SpecialistResult,
    SpecialistRunner,
    WalkerEvent,
)
from .skills import (
    Skill,
    SkillMatch,
    classify_match,
    deprecate_skill,
    find_matching_skills,
    list_archived_skills,
    list_skills,
    load_skill,
    record_skill_use,
    restore_skill,
    save_skill,
    similarity,
    skill_dir,
    tokenize,
)
from .skill_extraction import extract_skill, is_extraction_candidate
from .skill_manifest import (
    ManifestStatus,
    SkillManifest,
    SkillRequirement,
    check_manifest_status,
    manifest_path,
    read_manifest,
    save_current_skill_set,
    write_manifest,
)
from .autonomy import (
    AutonomySettings,
    FloorViolation,
    check_floor,
    DEFAULT_PROTECTED_BRANCHES,
    DEFAULT_PROTECTED_PATHS,
    DEFAULT_BUDGET_USD_MAX,
)
from .audit import (
    KIND_DECISION,
    KIND_FLOOR,
    KIND_PLAN_CHANGE,
    KIND_TOOL_CALL,
    append_event as append_audit_event,
    audit_path,
    log_decision,
    log_floor_violation,
    log_plan_change,
    log_tool_call,
    read_events as read_audit_events,
    stream_events as stream_audit_events,
)
from .runner import LocalSpecialistRunner
from .intent_service import IntentService

__all__ = [
    "PlanGraph",
    "PlanNode",
    "NodeStatus",
    "NodeSpecialization",
    "new_node_id",
    "save_graph",
    "load_graph",
    "snapshot_graph",
    "list_snapshots",
    "restore_snapshot",
    "purge_old_snapshots",
    "plans_dir",
    "SPECIALISTS",
    "SpecialistProfile",
    "get_specialist",
    "assemble_system_prompt",
    "filter_tools_for_specialist",
    "GraphWalker",
    "SpecialistResult",
    "SpecialistRunner",
    "WalkerEvent",
    "Skill",
    "SkillMatch",
    "classify_match",
    "deprecate_skill",
    "find_matching_skills",
    "list_archived_skills",
    "list_skills",
    "load_skill",
    "record_skill_use",
    "restore_skill",
    "save_skill",
    "similarity",
    "skill_dir",
    "tokenize",
    "extract_skill",
    "is_extraction_candidate",
    "ManifestStatus",
    "SkillManifest",
    "SkillRequirement",
    "check_manifest_status",
    "manifest_path",
    "read_manifest",
    "save_current_skill_set",
    "write_manifest",
    "AutonomySettings",
    "FloorViolation",
    "check_floor",
    "DEFAULT_PROTECTED_BRANCHES",
    "DEFAULT_PROTECTED_PATHS",
    "DEFAULT_BUDGET_USD_MAX",
    "KIND_DECISION",
    "KIND_FLOOR",
    "KIND_PLAN_CHANGE",
    "KIND_TOOL_CALL",
    "append_audit_event",
    "audit_path",
    "log_decision",
    "log_floor_violation",
    "log_plan_change",
    "log_tool_call",
    "read_audit_events",
    "stream_audit_events",
    "LocalSpecialistRunner",
    "IntentService",
]
