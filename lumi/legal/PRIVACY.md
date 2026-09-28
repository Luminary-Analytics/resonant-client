<!-- Rendered by packaging/legal_texts.py from lumi/legal/templates/PRIVACY.md and lumi/legal/terms.json. Edit those, then run: python packaging/legal_texts.py render -->
# Lumi Privacy Notice

Version 1.0, effective September 28, 2026

This notice describes what the Lumi desktop app and its command-line tools
send off your computer, where they send it and when, and what they keep on
your computer. [[TO BE PROVIDED: legal entity name]] ("Luminary") provides it. It doesn't
describe what your model providers, your organization, or other services you
connect Lumi to do with what they receive; their own policies do.

## In short

- Your prompts, code and files go to the model providers you choose, under
  your own accounts with them. Luminary doesn't receive them.
- Lumi's update checks go to Luminary's update site, which GitHub hosts,
  unless you turn them off.
- Luminary receives feedback only when you send it, and anything else only if
  you sign in to Lumi Cloud or your computer is enrolled in an organization.
- Lumi has no analytics, usage telemetry, advertising identifiers or automatic
  crash reporting.

## Update checks

- **Where:** Luminary's update site, `luminary-analytics.github.io`, which
  GitHub Pages hosts.
- **When:** about once a day while updates are automatic (the default); only
  when you choose Check for updates while they are manual; never while they
  are off (Settings > Updates, or your organization's policy). On Windows the
  checker starts with the app, the terminal interface and the chat gateway; on
  macOS, with the app. Copies installed with the MSI, the PKG, a .deb or an
  .rpm, and Lumi on Linux, don't check.
- **What:** a request for the update feed. Its address shows which feed Lumi
  reads: the one for your platform, your update channel and any release line
  you pinned. On Windows the request also names Lumi and its version and says
  it runs on 64-bit Windows. Like any web request, it shows your IP address to
  the host. It carries no account, device identifier or information about how
  you use Lumi.
- If you install an update, it's downloaded from the same site.

## Model requests

- **What:** to do what you ask, Lumi sends the model providers you configure
  your messages and the conversation so far; its instructions, including
  instruction files in your project (such as AGENTS.md); the contents of files
  and the command output the agent reads; and images you attach or
  screenshots the agent takes. It also sends shorter requests to the same
  providers to title conversations, summarize long ones and describe images.
- **Where:** only providers you set up, such as Anthropic, OpenAI (including
  Azure OpenAI), OpenRouter, Kimi, SONN, connections you add, and models on
  your computer or network (Ollama, EXO). Codex and Claude Code are separate
  programs that connect to OpenAI and Anthropic with your own sign-in.
  Provider extensions from capability packs you approve run as their own
  programs.
- **Model lists:** Lumi asks the providers you configured, and Ollama and EXO
  on your computer, which models they offer. When you have an OpenRouter key,
  it reads OpenRouter's public list of models.
- Before each request, Lumi removes the values of the keys you saved in it
  from what tools returned. It can also remove other values that look like
  secrets (Settings > Privacy & security), and it applies your organization's
  data loss prevention rules, if it has any.

## What the agent reaches for you

When you, or the agent at your request, use these features, they connect
where they need to:

- the web browser Lumi starts, to the pages it opens;
- code hosts and issue trackers you connect (GitHub, GitLab, Bitbucket, Azure
  DevOps, Jira, Linear), with the tokens you give Lumi;
- MCP servers you add; capability packs, which Lumi downloads with Git from
  the addresses you give; an Engram memory server, if you set one up; and an
  OpenTelemetry endpoint, if you export the audit log;
- the chat gateway, to Telegram or Slack;
- dictation: after you stop speaking, the audio goes to the transcription
  service you chose in Settings > Voice, or, with the window's built-in speech
  recognition, to the service of the browser engine that shows Lumi (Google
  or Microsoft on Windows, Apple on macOS). Lumi keeps no copy of the audio.

Commands and programs the agent runs connect wherever those programs do.

## Lumi Cloud

Lumi contacts Lumi Cloud only after you sign in to it, you join an
organization, or your organization's machine policy enrolls your computer.
Until then it has no Lumi Cloud address.

- **Signing in** happens in your web browser.
- **Enrolling** a computer sends its name, operating system and Lumi version,
  and a public key.
- **While enrolled,** Lumi checks in about once an hour with its version and
  platform, the version of the organization's policy, per-model counts of
  requests, tokens and cost, and counts of turns and how they ended (including
  files changed and crashes). Check-ins never carry prompts, code, file paths
  or titles.
- **Features you use** send what they need: a shared conversation (your
  messages, Lumi's replies and one line per action, never what tools
  returned, with secrets removed); a hand-off (the same, with your note and
  the repository's address, branch and commit); your organization's library of
  skills and prompts; reviews and second approvals of commands; and tasks from
  Slack or Microsoft Teams, if you turn them on.

## Feedback

Lumi sends feedback only when you choose Send in its feedback dialog, which
shows you the exact report first.

- **Every report** carries what you wrote, the reply address you give (if
  any), Lumi's version and update channel, your operating system and
  processor architecture, and a random identifier for your installation.
- **Diagnostics,** only if you turn on Include diagnostics (it starts off):
  the Python version and platform Lumi runs on, whether it runs from an
  installer, the kind of model provider and the model in use, whether offline
  mode is on, and the last lines of Lumi's startup log, with saved keys,
  values that look like secrets and your home folder's name removed. The log
  can contain parts of conversations.
- **Where:** Luminary's feedback inbox in Lumi Cloud, unless your
  organization's policy names another address or turns feedback off. When you
  open the dialog, Lumi may ask that inbox whether it accepts reports; that
  request contains nothing you wrote. A report that can't be sent yet waits on
  your computer.

## Organization oversight

Only if your organization's policy turns on oversight, and your computer is
enrolled in that organization's Lumi Cloud: Lumi first shows a notice that
names the organization and what it receives, and sends nothing to a model
until you confirm it. Then, for each turn, it sends that organization's Lumi
Cloud what its policy asks for: your sessions' activity (such as the project
folder's name, the model, the tools used, the outcome and the cost); if the
policy asks, your messages, Lumi's replies and your sessions' titles, with
secrets removed; and security flags. It never sends file contents, what tools
returned, screenshots or keystrokes. Settings > Privacy & security lists
exactly what your organization receives. Your organization decides what it
collects, how long it keeps it and who reads it.

## Offline mode

With offline mode on (Settings > Offline mode, or your organization's
policy), Lumi connects only to your computer and the hosts you allow, and
refuses everything else, including update checks, Lumi Cloud and feedback,
unless their hosts are allowed. It can't restrict the programs the agent runs.

## What stays on your computer

- **Lumi's folder,** `.lumi` in your home folder (or the folder
  `LUMI_STATE_HOME` names): settings; conversations and their transcripts;
  checkpoints, saved files and worktrees; logs; the audit log (kept for 365
  days by default); usage records; the profile of the browser Lumi starts;
  capability packs; records waiting to be sent; and a local copy of your
  organization's policy and library.
- **Keys and sign-ins:** in your operating system's credential store, under
  the name "Lumi" (Windows Credential Manager, the macOS Keychain or your
  Linux desktop's secret service). Where there is no credential store, they
  stay in Lumi's settings file.
- **In projects you open:** a `.lumi` folder (project notes, the code index,
  roadmaps and hand-offs), and Git branches and references that Lumi's
  worktrees and checkpoints use.
- **Elsewhere:** on Windows, the update checker's settings in the registry
  (`HKEY_CURRENT_USER\Software\Luminary Analytics\Lumi\WinSparkle`); on macOS,
  the updater's settings in the `com.luminaryanalytics.lumi` preferences; and
  any scheduled tasks you create in Lumi.
- Before Lumi was renamed, it kept its folder in `~/.resonant`; Lumi moves it
  to `~/.lumi` when it starts.

## Deleting your data

Uninstalling Lumi removes the app and keeps your data. To delete the data as
well, remove the `.lumi` folder in your home folder (and `.resonant`, if it's
still there), the "Lumi" entries in your credential store, and the `.lumi`
folders in your projects. Delete scheduled tasks in Lumi before you uninstall
it. What your model providers, your organization or Luminary already received
is deleted through them; to ask Luminary, write to [[TO BE PROVIDED: notices email address]].

## Changes and questions

Each version of this notice has a number and an effective date, comes with
Lumi (Settings > About Lumi, or `lumi terms show privacy`), and is announced in
the release notes. Questions about this notice: [[TO BE PROVIDED: notices email address]].
