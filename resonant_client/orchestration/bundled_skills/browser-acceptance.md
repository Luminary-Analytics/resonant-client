---
name: Browser acceptance and usability
description: Verify browser web applications with real input, keyboard navigation, persistence, responsive layout and error recovery.
triggers: [browser, web app, frontend, taskboard, dashboard, responsive, keyboard]
pinned: false
---
Use for an interactive browser app, after inspecting its actual scripts and stack.

1. Map the requested behaviors to a short acceptance checklist. Run automated checks using check_run with a requirement label.
2. Start the documented development command using preview_start, an argument array and a free loopback port. Confirm readiness. Read preview_status logs on failure; fix the cause instead of guessing detached-shell variations. The user can stop the project-owned preview later.
3. Use real browser input for the primary journey: create, edit, select, submit and remove where applicable. Exercise blank and invalid input, then a valid operation. Verify persistence after refresh and backend restart when promised.
4. Physically navigate using Tab, Space, Enter and Escape. Check focus remains on a useful control after rendering, editing, errors and submission. Native controls alone do not establish keyboard usability.
5. Set a genuinely narrow browser viewport and inspect overflow, navigation, form labels and touch targets. A desktop screenshot or responsive CSS alone does not verify this. Report unavailable browser capabilities as untested.
6. Let users choose business objects by names and useful context (SKU, availability), keeping database IDs internal. Errors should use the same names and appear beside affected inputs.
7. Report the preview URL, checked behaviors, failed checks and untested gaps. A page load or screenshot alone is not interaction evidence. Do not say fully tested.

Command recipes: inspect package.json and use its existing scripts. For Python use `python -m unittest discover -s tests` when appropriate. For Node's built-in test runner use `node --test` or explicit test file paths. On Windows the shell tool uses cmd.exe: write multiline Python to a file and run it; do not use Unix heredocs. preview_start bypasses the shell: pass arguments individually; on Windows invoke npm scripts through `["cmd", "/c", "npm", "run", "dev", "--", "--host", "127.0.0.1"]` only when the project provides that script.
