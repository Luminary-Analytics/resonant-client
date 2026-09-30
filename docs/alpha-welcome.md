# Welcome to the Lumi alpha

Lumi, from Luminary Analytics, is a coding agent for your desktop. Describe a
change: Lumi reads your code, edits files, runs your checks and shows what it
did and what passed, with the AI models and accounts you choose.

**Who it's for:** developers happy to try early software on code they can
review and undo. Windows comes first; the Mac version (Apple silicon) is a
preview. Free for individuals during the alpha.

## Install

- **Windows:** [download the installer](https://luminary-analytics.github.io/resonant-client/downloads/v0.20.0-alpha.1/lumi-setup-0.20.0-alpha.1.exe).
  It isn't code-signed yet: at "Windows protected your PC", choose **More
  info**, then **Run anyway**. Setup needs administrator rights.
- **Mac:** [download the disk image](https://luminary-analytics.github.io/resonant-client/downloads/v0.20.0-alpha.1/lumi-0.20.0-alpha.1.dmg)
  and drag Lumi to Applications. When macOS first blocks it, choose **Done**,
  then **System Settings › Privacy & Security › Open Anyway**.
- Accept the license and alpha terms when Lumi opens. For later alphas, set
  **Settings › Updates › Channel** to **Beta**.

## Try first

1. Connect a model: an Anthropic, OpenAI or OpenRouter key, a ChatGPT or
   Claude subscription through the Codex or Claude Code CLI, or a local
   Ollama model.
2. Open a project (or the sample project) and ask for a small fix. New
   installs start in Auto-edit: file edits apply, commands ask first.
   **Timeline** restores checkpoints.
3. Try **Team** (preview): type `/team` and a goal, and an orchestrator runs
   parallel workers. For now, use OpenRouter, Ollama or another
   OpenAI-compatible model.

## Feedback

Email rich.bellantoni@luminaryanalytics.com. Until our feedback service
opens, **Help › Send Feedback** only saves reports on your computer; copy them
into your email.

## Your data

Your prompts, code, files and the agent's screenshots go only to the model
providers you choose. Besides update checks, which you can turn off, Lumi
sends no analytics, telemetry or crash reports. Read the
[privacy notice](https://github.com/Luminary-Analytics/resonant-client/blob/v0.20.0-alpha.1/lumi/legal/PRIVACY.md).
