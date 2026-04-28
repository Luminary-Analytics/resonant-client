# PLAN — Computer-Use Upgrades

> Status: ✅ Shipped · Tasks: 8 / 8 · Last verified: 2026-04-26

## Objective

Make the agent's `computer_*` tools dramatically more reliable and capable for automating real Windows / macOS apps. The pre-existing flow clicked at pixel coordinates against a full-desktop screenshot — fragile against DPI, multi-monitor, theme, and "the user moved the window."

What this cluster delivered:

- Window-relative and monitor-relative coordinate systems
- Semantic targeting via the OS accessibility tree (Win32 UIA / macOS AXUIElement)
- A region-aware wait-for-change primitive (`computer_wait` mode=`change` + `region`)
- Three net-new tools: clipboard text, process management, screen recording
- A pixel-diff tool (`screen_diff`) that returns changed-region rectangles for the preview overlay

Result: the agent drives any GUI app with the same reliability the browser tools already had.

## Context

Files a future executor (or anyone extending this cluster) must read first:

- [ARCHITECTURE.md](ARCHITECTURE.md) — `engine/computer.py` and `engine/computer_use.py` summary
- [resonant_client/engine/tools.py](resonant_client/engine/tools.py) — `AGENT_TOOLS` (line 22), `TOOL_ICONS` mapping, `execute_tool()` dispatch
- [resonant_client/engine/sandbox.py](resonant_client/engine/sandbox.py) — `READ_ONLY_TOOLS`, `FILE_WRITE_TOOLS`, permission categories
- [resonant_client/engine/computer.py](resonant_client/engine/computer.py) — basic screenshot/click/type/scroll, `_resolve_target_region()` precedence helper
- [resonant_client/engine/computer_use.py](resonant_client/engine/computer_use.py) — windows, drag, hover, wait, OCR, app launcher, `get_window_rect()`
- [resonant_client/engine/accessibility.py](resonant_client/engine/accessibility.py) — `get_tree()`, `find_element()`, `click_element()`
- [resonant_client/engine/clipboard.py](resonant_client/engine/clipboard.py) — text + image clipboard helpers
- [resonant_client/engine/processes.py](resonant_client/engine/processes.py) — `list_processes()`, `kill_process()`
- [resonant_client/engine/recording.py](resonant_client/engine/recording.py) — `Recorder` class, `~/.resonant/recordings/` output
- [resonant_client/engine/screen_diff.py](resonant_client/engine/screen_diff.py) — `diff_images()` connected-component rectangle extraction

## Prior art (do NOT reinvent)

| Feature | Where it lives now |
|---|---|
| `screen_ocr` (region OCR) | [engine/computer_use.py](resonant_client/engine/computer_use.py) → tool name `screen_ocr` |
| `open_application` (app launcher) | [engine/computer_use.py](resonant_client/engine/computer_use.py) → tool name `open_application` |
| `window_list`, `window_focus` | [engine/computer_use.py](resonant_client/engine/computer_use.py) |
| `computer_wait` mode=`change` (full screen + region) | [engine/computer_use.py](resonant_client/engine/computer_use.py) |
| Image clipboard read (Ctrl+V paste path) | [engine/clipboard.py](resonant_client/engine/clipboard.py) |
| `with_auto_screenshot` post-action wrapper | [engine/computer_use.py](resonant_client/engine/computer_use.py) |
| `get_window_rect()` returning client rect | [engine/computer_use.py](resonant_client/engine/computer_use.py) |
| `_resolve_target_region()` precedence (region > target_window > monitor > primary) | [engine/computer.py:126](resonant_client/engine/computer.py:126) |

If you ever try to "add" one of these, **stop and re-read the existing implementation first**.

## Tasks

All eight tasks below are ✅ shipped. Each line points to the implementing files and a verify command that passes against the repo today.

---

### Task 1.1 — Region support for `computer_wait` (mode=change) ✅ Shipped

**Lives in:**
- [resonant_client/engine/computer_use.py](resonant_client/engine/computer_use.py) — `exec_computer_wait` accepts `region: {x,y,width,height}`
- [resonant_client/engine/tools.py](resonant_client/engine/tools.py) — `computer_wait` schema includes `region` object
- [tests/test_computer_use_upgrades.py](tests/test_computer_use_upgrades.py) — `test_computer_wait_region_*`

**Verify:**
```bash
pytest tests/test_computer_use_upgrades.py -k computer_wait -v
```

**Done when (✅):** A region-scoped wait triggers within timeout for changing pixels and times out for static ones.

---

### Task 1.2 — Window-targeted screenshots and clicks ✅ Shipped

**Lives in:**
- [resonant_client/engine/computer.py:126](resonant_client/engine/computer.py:126) — `_resolve_target_region(args)` (precedence: target_window > monitor > primary)
- [resonant_client/engine/computer.py](resonant_client/engine/computer.py) — `exec_computer_screenshot` and `exec_computer_click` honor `target_window` and translate window-relative `(x,y)` to screen coords
- [resonant_client/engine/computer_use.py](resonant_client/engine/computer_use.py) — `get_window_rect(title_substring)` (Win32 + macOS + Linux paths)
- [resonant_client/engine/tools.py](resonant_client/engine/tools.py) — `computer_screenshot` / `computer_click` schemas include `target_window: string`

**Verify:**
```bash
pytest tests/test_computer_use_upgrades.py -k window -v
# Manual on Windows: open Notepad
python -c "from resonant_client.engine.computer import exec_computer_screenshot; \
  print(exec_computer_screenshot({'target_window':'Notepad'}).text[:200])"
```

**Done when (✅):** Screenshot of a known window matches its client-area dimensions ± 2px. A click at `(10, 10)` with `target_window='...'` lands inside the window, not on the desktop.

---

### Task 1.3 — Multi-monitor support ✅ Shipped

**Lives in:**
- [resonant_client/engine/tools.py](resonant_client/engine/tools.py) — `monitors_list` tool registered (line ~822)
- [resonant_client/engine/computer.py](resonant_client/engine/computer.py) — `exec_monitors_list` returns `[{index, left, top, width, height, primary}]`; screenshot/click accept `monitor: int`
- [resonant_client/engine/sandbox.py](resonant_client/engine/sandbox.py) — `monitors_list` is in `READ_ONLY_TOOLS`

**Verify:**
```bash
pytest tests/test_computer_use_upgrades.py -k monitors -v
python -c "from resonant_client.engine.computer import exec_monitors_list; \
  print(exec_monitors_list({}).text)"
```

**Done when (✅):** `monitors_list` returns the correct count and primary flag. `computer_screenshot(monitor=N)` and `computer_click(monitor=N)` respect the index.

---

### Task 1.4 — Accessibility-tree targeting (UIA / AX) ✅ Shipped

**Lives in:**
- [resonant_client/engine/accessibility.py](resonant_client/engine/accessibility.py) — `get_tree`, `find_element`, `click_element`
- [resonant_client/engine/tools.py](resonant_client/engine/tools.py) — `accessibility_tree` and `accessibility_click` tools registered
- [resonant_client/engine/sandbox.py](resonant_client/engine/sandbox.py) — `accessibility_tree` read-only; `accessibility_click` same category as `computer_click`
- [tests/test_computer_use_upgrades.py](tests/test_computer_use_upgrades.py) — accessibility tests gated on `uiautomation` import

**Verify:**
```bash
pytest tests/test_computer_use_upgrades.py -k accessibility -v
# Manual on Windows (after `pip install uiautomation`): open Calculator, then
python -c "from resonant_client.engine.accessibility import find_element, click_element; \
  el = find_element({'window_title':'Calculator','role':'Button','name':'7'}); \
  print(click_element(el))"
```

**Done when (✅):** Tree returns elements with semantic `role`/`name`/`bounds`. `find_element` + `click_element` round-trip works on Calculator. Falls back gracefully when `uiautomation` isn't installed.

---

### Task 1.5 — Clipboard text read/write tools ✅ Shipped

**Lives in:**
- [resonant_client/engine/clipboard.py](resonant_client/engine/clipboard.py) — `read_clipboard_text` / `write_clipboard_text` (pyperclip primary, OS-shell fallbacks)
- [resonant_client/engine/tools.py](resonant_client/engine/tools.py) — `clipboard_read` / `clipboard_write` registered (line ~830)
- [resonant_client/engine/sandbox.py](resonant_client/engine/sandbox.py) — `clipboard_read` read-only; `clipboard_write` modify-state

**Verify:**
```bash
pytest tests/test_computer_use_upgrades.py -k clipboard -v
python -c "from resonant_client.engine.clipboard import write_clipboard_text, read_clipboard_text; \
  write_clipboard_text('hello'); print(read_clipboard_text())"
```

**Done when (✅):** Round-trip succeeds on the dev machine. Both tools dispatch through `execute_tool`.

---

### Task 1.6 — Process management tools ✅ Shipped

**Lives in:**
- [resonant_client/engine/processes.py](resonant_client/engine/processes.py) — `list_processes`, `kill_process`, `SYSTEM_PID_FLOOR`, `NEVER_KILL_NAMES`
- [resonant_client/engine/tools.py](resonant_client/engine/tools.py) — `process_list` / `process_kill` registered (line ~852)
- [resonant_client/engine/sandbox.py](resonant_client/engine/sandbox.py) — `process_list` read-only; `process_kill` always-ask outside `bypass`

**Verify:**
```bash
pytest tests/test_computer_use_upgrades.py -k process -v
```

**Done when (✅):** Guardrails reject system PIDs and `os.getpid()`. `process_kill` always prompts in non-`bypass` modes.

---

### Task 1.7 — Screen recording ✅ Shipped

**Lives in:**
- [resonant_client/engine/recording.py](resonant_client/engine/recording.py) — `Recorder` (cv2 primary, ffmpeg fallback), one-active-per-process lock, `~/.resonant/recordings/`
- [resonant_client/engine/tools.py](resonant_client/engine/tools.py) — `screen_record_start` / `screen_record_stop` registered (line ~883)
- [resonant_client/gui/app.py](resonant_client/gui/app.py) — pushes `recording_status` event over WebSocket on start/stop
- [resonant_client/gui/static/app.js](resonant_client/gui/static/app.js) — header badge "● REC" appears while active

**Verify:**
```bash
pytest tests/test_computer_use_upgrades.py -k record -v
```

**Done when (✅):** Recording produces a playable MP4 in `~/.resonant/recordings/`. Header badge appears/disappears on start/stop.

---

### Task 1.8 — Visual diff overlay between screenshots ✅ Shipped

**Lives in:**
- [resonant_client/engine/screen_diff.py](resonant_client/engine/screen_diff.py) — `diff_images(prev_png, curr_png, threshold)` returns `{rects, changed_pixel_pct, size}`
- [resonant_client/engine/computer_use.py](resonant_client/engine/computer_use.py) — `with_auto_screenshot` stashes the previous screenshot bytes for the LRU
- [resonant_client/engine/tools.py](resonant_client/engine/tools.py) — `screen_diff` registered (line ~913)
- [resonant_client/gui/static/app.js](resonant_client/gui/static/app.js) — preview-panel overlay renderer for `screen_diff` results
- [resonant_client/gui/static/styles.css](resonant_client/gui/static/styles.css) — `.diff-rect` overlay style

**Verify:**
```bash
pytest tests/test_computer_use_upgrades.py -k diff -v
```

**Done when (✅):** Synthetic test detects a known changed rect; manual Notepad-open test shows the overlay covering Notepad's window region.

---

## Overall verification

```bash
cd D:/Repos/resonant-client

# Full cluster suite
python -m pytest tests/test_computer_use_upgrades.py tests/test_computer_use.py -q

# Tool registry sanity check (every cluster tool present)
python -c "from resonant_client.engine import tools; \
  names = {t['function']['name'] for t in tools.AGENT_TOOLS}; \
  required = {'monitors_list','accessibility_tree','accessibility_click', \
              'clipboard_read','clipboard_write','process_list','process_kill', \
              'screen_record_start','screen_record_stop','screen_diff'}; \
  missing = required - names; \
  assert not missing, f'missing: {missing}'; \
  print('all 10 cluster tools registered (', len(names), 'tools total)')"
```

Smoke test in the running app:

1. `python -m resonant_client.gui.server --port 8765 --browser`
2. Open Notepad. Ask the agent: *"use accessibility tools to find the text area in Notepad and type 'hello world'"*. Confirm it works without pixel coordinates.
3. Ask: *"screenshot just the Notepad window"*. Confirm the returned screenshot is the window only (target_window precedence).

## Success criteria

- [x] All 10 new/extended tools appear in `AGENT_TOOLS` and dispatch from `execute_tool`.
- [x] `pytest tests/test_computer_use_upgrades.py` is green (25 tests).
- [x] Each new tool has at least one regression test.
- [x] Sandbox classifications correct: read-only tools never prompt in `auto-edit`; destructive tools always prompt outside `bypass`.
- [x] No new tool added to `READ_ONLY_TOOLS` if it can modify state.

## Future / nice-to-haves (not yet built)

| Idea | Where it would go | Why it's not built yet |
|------|-------------------|-----------------------|
| `accessibility_type(element, text)` — type into an element by AX role/name | `engine/accessibility.py` | Today the agent uses `accessibility_click` then `computer_type` — works but two calls |
| WebRTC screen recording for browser mode | `engine/recording.py` browser path | The MP4 recorder requires desktop access; browser sessions can't record |
| Diff-overlay on **window** screenshots, not just full-screen | `screen_diff.py` + `computer.py` | Current overlay coords assume full-screen; window-relative needs a small translation pass |
| OCR + accessibility-tree merge — return text for elements that lack `name` | `engine/accessibility.py` calls `screen_ocr` on the bounds | Would help legacy Win32 controls that expose blank `name` attributes |

## Output

When extending this cluster, append a SUMMARY-style entry below this section instead of writing a separate file:

> 2026-04-26 — All 8 tasks shipped. 25 tests pass. No deviations from this plan.
