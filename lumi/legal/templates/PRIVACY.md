# {{doc.privacy.title}}

Version {{doc.privacy.version}}, published {{doc.privacy.published}}

This notice describes what the Lumi desktop app and its command-line tools
send off your computer, where they send it and when, and what they keep on
your computer. {{entity.legal_name}} ("Luminary") provides it. It doesn't
describe what your model providers, your organization, or other services you
connect Lumi to do with what they receive; their own policies do.

## In short

- Your prompts, code and files go to the model providers you choose, under
  your own accounts with them. A model request doesn't go to Luminary.
- If your organization's policy names a data loss prevention service, that
  service receives the text of your model requests first, to check them.
- Lumi's update checks go to Luminary's update site, which GitHub hosts,
  unless you turn them off.
- Luminary's Lumi Cloud receives anything only once you sign in to it or your
  computer is enrolled in an organization: what you choose to share, hand
  off or send through it, what your organization's policy asks for, and,
  from an enrolled computer, hourly counts of usage and crashes. Luminary
  receives feedback only when you send it.
- Lumi has no advertising identifiers. It sends no analytics, usage
  telemetry or crash reports, except the hourly counts a computer enrolled in
  an organization reports to Lumi Cloud, and the audit log if you export it to
  an OpenTelemetry endpoint of your own.

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

Lumi sends no model request until its terms have been accepted.

- **What:** to do what you ask, Lumi sends the model providers you configure
  your messages and the conversation so far; its instructions, including
  instruction files in your project (such as AGENTS.md); the contents of files
  and the command output the agent reads; and images you attach or
  screenshots the agent takes, including those of computer use, which is on
  unless you or your organization turn it off. It also sends shorter requests
  to the same providers to title conversations, summarize long ones and
  describe images, and a short request to load a model before your first
  message.
- **Where:** only providers you set up, such as Anthropic, OpenAI (including
  Azure OpenAI), OpenRouter, Kimi, SONN, connections you add, and models on
  your computer or network (Ollama, EXO). Codex and Claude Code are separate
  programs that connect to OpenAI and Anthropic with your own sign-in.
  Provider extensions from capability packs you approve run as their own
  programs.
- **Model lists:** Lumi asks the providers you configured, and Ollama and EXO
  on your computer, which models they offer. When you have an OpenRouter key,
  it reads OpenRouter's public list of models.
- **Secrets and your organization's rules:** before each request, Lumi
  removes the values of the keys you saved in it from what tools returned. It
  can also remove other values that look like secrets (Settings > Privacy &
  security). If your organization's policy has data loss prevention rules,
  Lumi applies them. If the policy also names a data loss prevention service,
  Lumi sends that service the text of each request, after its own
  redactions, with the organization's name and the provider, model and
  purpose of the request, and sends the request only as the service allows.
  Your organization chooses and runs that service.

## What the agent reaches for you

When you, or the agent at your request, use these features, they connect
where they need to:

- the web browser Lumi starts, to the pages it opens;
- code hosts and issue trackers you connect (GitHub, GitLab, Bitbucket, Azure
  DevOps, Jira, Linear), with the tokens you give Lumi;
- MCP servers you add; capability packs, which Lumi downloads with Git from
  the addresses you give; an Engram memory server, if you set one up, which
  receives what Lumi recalls and remembers; and an OpenTelemetry endpoint, if
  you export the audit log;
- the chat gateway, to Telegram or Slack;
- dictation: after you stop speaking, the audio goes to the transcription
  service you chose in Settings > Voice, or, with the window's built-in speech
  recognition, to the service of the browser engine that shows Lumi (Google
  or Microsoft on Windows, Apple on macOS). Lumi keeps no copy of the audio.

Commands and programs the agent runs connect wherever those programs do.

## Lumi Cloud

Lumi Cloud is Luminary's online service for Lumi. Lumi contacts it only after
you sign in to it, you join an organization, or your organization's machine
policy enrolls your computer. Until then it has no Lumi Cloud address. What
Lumi Cloud keeps for an organization, and for how long, is governed by the
organization's agreement with Luminary.

- **Signing in** happens in your web browser. Lumi keeps the sign-in in your
  credential store.
- **Enrolling** a computer sends its name, operating system and Lumi version,
  and a public key.
- **While enrolled,** Lumi checks in about once an hour with its version and
  platform, the version of the organization's policy, per-model counts of
  requests, tokens and cost (naming each provider and model), and counts of
  turns and how they ended, of checks that passed, of files changed and of
  crashes of the app. Check-ins never carry prompts, code, file paths or
  titles. Lumi also downloads the organization's policy.
- **Features you use** send what they need:
  - a shared conversation: your messages, Lumi's replies and one line per
    action (the tool and what it acted on), never what tools returned, with
    saved keys and values that look like secrets removed;
  - a hand-off: the same, with your note and the repository's address,
    branch and commit;
  - a second person's approval of a command: the command, the tool and the
    project;
  - pull requests the agent opens, when your organization reviews agent
    changes;
  - your organization's library: Lumi downloads its skills, prompts and
    project notes, and sends a project note you propose;
  - tasks from Slack or Microsoft Teams, if you turn them on: Lumi takes each
    request from Lumi Cloud and sends back Lumi's reply, or what went wrong,
    and a description of each action it asks the chat to approve.
- **Your organization's oversight,** if its policy turns it on (below).

## Feedback

When the feedback feature is available in your version of Lumi, you can send
feedback from its dialog, which shows you the exact report first. Lumi sends
a report only after you choose Send.

- **Every report** carries what you wrote, its kind (a bug, an idea or
  other), the reply address you give (if any), Lumi's version and update
  channel, your operating system and processor architecture, and an
  identifier for your installation. That identifier is derived from a random
  value kept on your computer, and differs for each place reports go and for
  each account, so reports sent without your account can't be linked to
  those sent with it.
- **Diagnostics,** only if you turn them on (they start off) and your
  organization allows them: the Python version and platform Lumi runs on,
  whether it runs from an installer, the kind of model provider and the model
  in use (never a key), whether offline mode is on, and the last lines of
  Lumi's startup log, with saved keys, values that look like secrets and your
  home folder's name removed. The log can contain parts of conversations.
- **Where:** the feedback address set in Settings or by your organization's
  policy (`privacy.feedback_url`), or else the Lumi Cloud your computer uses.
  A report goes only where it was addressed when you wrote it. When you open
  the dialog, Lumi may ask that address whether it takes reports; that
  request contains nothing you wrote.
- **Your account:** while you're signed in to that Lumi Cloud, a report
  carries your sign-in, so it's linked to your account there; you can
  choose to send a waiting report without it.
- **Later:** a report that can't be sent at once waits on your computer, and
  Lumi tries again in the background until it's delivered, refused, or you
  discard it. After 30 days a waiting report isn't sent any more; you can copy
  or discard it.
- **Your organization:** its data loss prevention rules check a report before
  it goes, and its policy can name where reports go, or turn diagnostics, or
  feedback itself, off.

## Organization oversight

Only if your organization's policy turns on oversight, and your computer is
enrolled in that organization's Lumi Cloud:

- **The notice.** For work you start yourself (in the app, at a terminal or
  through the chat gateway), Lumi first shows a notice that names the
  organization and what it receives, and sends nothing to a model until you
  confirm it. Your confirmation is signed with your computer's key and sent to
  the organization's Lumi Cloud. Work that runs with nobody there to see the
  notice, such as a scheduled task or `lumi run` without a terminal, is
  recorded as yours if you confirmed the notice on this computer. Otherwise,
  by default, it runs without asking and is recorded as unattended, with the
  notice in its output; the policy can refuse such work instead.
- **What goes, for each turn:** your sessions' activity (such as the
  project folder's name, your computer user name, the provider and model,
  the permission mode, the tools used and whether they ran, the outcome and
  the cost); if the policy asks, your messages, Lumi's replies, your
  sessions' titles and the commands, paths and patterns the tools were given,
  with secrets removed; and security flags, such as a refused command or text
  in a tool's output that tried to instruct the agent. When your messages are
  shared, a flag carries a short excerpt of the text that raised it, which
  can come from what a tool returned (a web page, a file, a command's
  output), with secrets removed and your organization's data loss prevention
  rules applied.
- **What never goes:** the contents of files and what tools returned (beyond
  those excerpts), screenshots, keystrokes, or anything outside Lumi's own
  turns.

Settings > Privacy & security lists exactly what your organization receives.
Your organization decides what it collects, how long it keeps it and who
reads it.

## Offline mode

With offline mode on (Settings > Offline mode, or your organization's
policy), Lumi connects only to your computer and the hosts you allow, and
refuses everything else, including update checks, Lumi Cloud and feedback,
unless their hosts are allowed. It can't restrict the programs the agent runs.

## What stays on your computer

- **Lumi's folder,** `.lumi` in your home folder (or the folder
  `LUMI_STATE_HOME` names): settings; conversations and their transcripts;
  checkpoints, saved files and worktrees; logs; the audit log (kept for 365
  days by default); usage records and counts of turns and crashes; the
  profile of the browser Lumi starts; capability packs; the record of your
  acceptance of Lumi's terms; records and reports waiting to be sent; and a
  local copy of your organization's policy and library.
- **Keys and sign-ins:** in your operating system's credential store, under
  the name "Lumi" (Windows Credential Manager, the macOS Keychain or your
  Linux desktop's secret service). Where there is no credential store, they
  stay in Lumi's settings file.
- **In projects you open:** a `.lumi` folder (project notes, the code index,
  roadmaps and hand-offs), and Git branches and references that Lumi's
  worktrees and checkpoints use.
- **Elsewhere:** on Windows, the update checker's settings in the registry
  (`HKEY_CURRENT_USER\Software\Luminary Analytics\Lumi\WinSparkle`), and what
  the installers note for themselves under `HKEY_LOCAL_MACHINE\SOFTWARE\Luminary Analytics\Lumi`
  (which versions of the terms the installer's license page showed, and the
  MSI's settings); on macOS, the updater's settings in the
  `com.luminaryanalytics.lumi` preferences; and any scheduled tasks you
  create in Lumi.
- Before Lumi was renamed, it kept its folder in `~/.resonant`; Lumi moves it
  to `~/.lumi` when it starts.

## Deleting your data

Uninstalling Lumi removes the app and keeps your data. To delete the data as
well, remove the `.lumi` folder in your home folder (and `.resonant`, if it's
still there), the "Lumi" entries in your credential store, and the `.lumi`
folders in your projects. Delete scheduled tasks in Lumi before you uninstall
it. What your model providers, your organization or Luminary already received
is deleted through them; to ask Luminary, write to {{notices_email}}.

## Changes and questions

Each version of this notice has a number and a publication date, comes with
Lumi (Settings > About Lumi, or `lumi terms show privacy`), and is announced
in the release notes. A new version doesn't ask you to accept anything.
Questions about this notice: {{notices_email}}.
