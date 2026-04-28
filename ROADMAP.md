# Resonant Client — Roadmap

Post-refocus, the client is a single-purpose agentic coder: Ollama + `deepseek-v4-flash:cloud` on the Mac Studio (`10.0.0.133`), with a clean Agent + Settings UI.

## Status (2026-04-28)

| # | Cluster | Plan | Status | Tasks shipped | Notes |
|---|---------|------|--------|---------------|-------|
| 1 | Computer-Use upgrades | [PLAN-COMPUTER-USE.md](PLAN-COMPUTER-USE.md) | ✅ Shipped | 8 / 8 | All tools registered in `engine/tools.py`; 25 tests in `tests/test_computer_use_upgrades.py` pass |
| 2 | Session ergonomics | [PLAN-SESSION-ERGONOMICS.md](PLAN-SESSION-ERGONOMICS.md) | ✅ Shipped | 4 / 4 | Fork + inline-diff + replay-scrubber + voice all wired in `gui/static/app.js` |
| 3 | deepseek-v4-flash specific | [PLAN-DEEPSEEK.md](PLAN-DEEPSEEK.md) | ✅ Shipped | 3 / 3 | Thinking-mode toggle + big-context profile + `get_runtime_telemetry()` |
| 4 | Codebase intelligence | [PLAN-CODEBASE-INTELLIGENCE.md](PLAN-CODEBASE-INTELLIGENCE.md) | ✅ Shipped | 4 / 4 | Auto-lint + auto-test + 5 git tools + 6 REPL tools, all gated by settings |
| 5 | Harness migration | [PLAN-HARNESS-MIGRATION.md](PLAN-HARNESS-MIGRATION.md) | ✅ Shipped | 5 / 5 | Sprint workflow now opt-in (default off), state moved to `~/.resonant/projects/<hash>/harness/`, AGENTS.md adopted as primary project-conventions file |
| 6 | Organic orchestration | [PLAN-ORGANIC-ORCHESTRATION.md](PLAN-ORGANIC-ORCHESTRATION.md) | ✅ Shipped | 5 / 5 | Five primitives (Intent · Plan-graph · Specialist · Reflection · Skill library); live plan-graph viz in preview panel; full autonomy with irreversibility-floor checkpoints + per-intent audit log |
| 7 | Intent wiring (live flow) | [PLAN-INTENT-WIRING.md](PLAN-INTENT-WIRING.md) | ✅ Shipped | 5 / 5 | `LocalSpecialistRunner` + `IntentService` connect orchestration to user input; `/plan` slash-prefix and "Plan this" button kick off real intents; floor enforcement + audit log fire from inside live tool dispatch; e2e stub-backend test passes |
| 8 | **Distribution: Windows installer + auto-update** | [RELEASING.md](RELEASING.md) + [docs/release-pipeline.md](docs/release-pipeline.md) | ✅ Shipped (v0.2.0) | 4 / 4 | PyInstaller bundle + Inno Setup installer (~26 MB) + WinSparkle auto-update + EdDSA signing + GitHub Pages appcast + tag-push CI workflow |

**Total: 38 / 38 atomic tasks shipped** across the 8 clusters.

**Released artifacts:**

- v0.2.0 installer: <https://github.com/Luminary-Analytics/resonant-client/releases/tag/v0.2.0>
- Auto-update channel: <https://luminary-analytics.github.io/resonant-client/appcast.xml>

End-to-end verification:

```bash
cd D:/Repos/resonant-client

# All cluster tests in one run (123 tests)
python -m pytest tests/test_computer_use_upgrades.py \
                  tests/test_deepseek_specific.py \
                  tests/test_session_ergonomics.py \
                  tests/test_git_tools.py \
                  tests/test_repl.py \
                  tests/test_auto_lint.py \
                  tests/test_auto_test.py -q

# Tool registry sanity check (52 tools)
python -c "from resonant_client.engine import tools; \
  names = sorted({t['function']['name'] for t in tools.AGENT_TOOLS}); \
  print(len(names), 'tools'); print('\n'.join(names))"
```

## How to read each plan

Each `PLAN-*.md` has:

- **Objective** — what and why the cluster exists
- **Context** — files and functions a future executor needs to read first
- **Prior art** — things that already exist; do NOT reinvent
- **Tasks** — atomic units with files, action, verify, done-when, plus a ✅/⚠️/⏳ status marker
- **Overall verification** — copy-paste commands that work against the repo today
- **Success criteria** — measurable, used to confirm "done"
- **Future / nice-to-haves** — ideas not yet built; pick one to extend the cluster

Status markers used inside each plan:

| Marker | Meaning |
|--------|---------|
| ✅ Shipped | Lives in `main`; tests cover it; verify command passes |
| ⚠️ Partial | Some sub-piece works; gap noted in "What's missing" line |
| ⏳ Pending | Not started; ready to execute |

## Recommended order (for re-executing from scratch)

The clusters are independent. If you needed to rebuild the whole feature surface in a new repo, the leverage-per-task ordering would be:

1. **Codebase intelligence** first — auto-lint and first-class git tools are productivity multipliers for every subsequent phase.
2. **Computer-Use upgrades** second — biggest visible capability boost. Accessibility-tree targeting (Task 1.4) is the foundation that makes later automations of real Windows apps reliable.
3. **deepseek-v4-flash specific** third — quality-of-life on the model layer. Smaller, benefits from a stable foundation.
4. **Session ergonomics** fourth — UI polish. Inline diff and session replay are nice but not load-bearing.

Within a cluster, tasks can typically be done in any order. Hard dependencies are called out per-task.

## Out of scope (deferred or rejected)

- **Mobile app** — not a target. Desktop / browser only.
- **Multi-user collaboration** — single-user IDE.
- **Plugin marketplace** — MCP servers (already wired) cover the extension story.
- **Cloud sync of sessions** — sessions stay in `~/.resonant/projects/`. If you want sync, point Dropbox/iCloud at that folder.
- **Cross-LLM tool proxying** — every tool runs on the same machine as the engine; no remote tool execution.

## Living document

When a new task is added to a cluster, append it to the plan and bump the count in the table above. When a cluster spawns a follow-up cluster (e.g., "agent-driven UI testing"), add a new row.
