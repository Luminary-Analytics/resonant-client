# Chat gateway: Telegram and Slack

`lumi gateway` lets you work with the Lumi agent from Telegram or Slack while
you're away from your computer. The computer running the gateway does the
work: the gateway runs there, on one project, and replies in the chat.

- Messages you send become requests to the agent, with one conversation per
  chat.
- When the agent wants to do something the permission mode doesn't allow on
  its own, it asks in the chat, with **Approve** and **Deny** buttons.
- **stop** stops the request that's running, and **status** says what's
  happening.

## Start it

```bash
lumi gateway --project ~/code/app
```

```bash
lumi gateway --channel slack --project ~/code/app --mode auto-edit
```

| Option | Default | |
| --- | --- | --- |
| `--channel` | `telegram`, or `slack` when only Slack's tokens are set | Which chat app. |
| `--project` | `gateway.project`, else the current folder | The project the agent works in. |
| `--mode` | `gateway.mode`, else `ask` | `ask`: asks in the chat before changing anything. `auto-edit`: edits files, asks about the rest. `bypass`: asks about nothing. |
| `--backend`, `--model` | `gateway.backend` and `gateway.model`, else your default model | The provider (`anthropic`, `openai`, `ollama`, `conn-<id>`, ...) and model. |
| `--allow` | `gateway.allowed_chat_ids` (Telegram) or `gateway.slack_allowed` (Slack) | Who may use it. Repeat for more than one. |
| `--approval-minutes` | `gateway.approval_minutes`, else 10 | How long to wait for an answer before refusing. |

The settings live in the `gateway` section of `~/.lumi/settings.json`, and
the tokens in its `api_keys` section (`telegram_bot`, `slack_bot`,
`slack_app`), or in the environment. The gateway gives whoever can write in
an allowed chat control of your computer, so both are edited in the file and
never from Lumi's Settings page. Lumi moves a token you put in the file to
your system's credential store the next time it, or the gateway, starts.

## In the chat

Send a request as you would in Lumi. Send these on their own, with or without
a leading slash (in Slack, without: Slack keeps the slash for its own
commands):

| Command | |
| --- | --- |
| `status` | The project, permission mode and model, and whether a request is running or waiting for you. |
| `stop` | Stop the request that's running. |
| `approve`, `deny` | Answer the waiting approval. The buttons do the same. |
| `clear` | Start a fresh conversation. |
| `help` | The list of commands. |

A request that arrives while another is running waits its turn: requests run
one at a time on the computer. An approval nobody answers in time is refused,
and the agent is told it wasn't done.

## Telegram

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. Put it in `~/.lumi/settings.json` as `"api_keys": {"telegram_bot": "..."}`
   (or set `TELEGRAM_BOT_TOKEN`, or pass `--token`).
3. Start the gateway and send the bot a message. It replies with your chat ID
   and runs nothing. Add the ID to `gateway.allowed_chat_ids`, or pass
   `--allow <id>`, and restart the gateway.

`gateway.telegram_api_url` points at a
[self-hosted Bot API server](https://github.com/tdlib/telegram-bot-api)
instead of `api.telegram.org`.

## Slack

The gateway uses Socket Mode: it opens a connection to Slack from your
computer, so it needs no public address.

1. At [api.slack.com/apps](https://api.slack.com/apps), choose **Create New
   App > From an app manifest**, pick your workspace, and paste:

   ```yaml
   display_information:
     name: Lumi
   features:
     app_home:
       messages_tab_enabled: true
       messages_tab_read_only_enabled: false
     bot_user:
       display_name: Lumi
   oauth_config:
     scopes:
       bot: [chat:write, im:history, channels:history, groups:history]
   settings:
     event_subscriptions:
       bot_events: [message.im, message.channels, message.groups]
     interactivity:
       is_enabled: true
     socket_mode_enabled: true
   ```

2. Install the app to the workspace and copy the **Bot User OAuth Token**
   (`xoxb-...`).
3. Under **Basic Information > App-Level Tokens**, generate a token with the
   `connections:write` scope (`xapp-...`).
4. Put both in `~/.lumi/settings.json` as `api_keys.slack_bot` and
   `api_keys.slack_app`, or set `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN`.
5. Start the gateway with `--channel slack`, and message the app directly.
   It replies with your user ID and the channel ID, and runs nothing. Add one
   to `gateway.slack_allowed` (or pass `--allow`), and restart the gateway.

In a direct message every message goes to the agent. In a channel, invite
the app and mention it: `@Lumi run the tests`. An allowed **user ID** lets
that person use the agent anywhere the app is. An allowed **channel ID** lets
everyone in that channel use it.

## Microsoft Teams

Teams delivers bot messages only to a public HTTPS address, which a gateway
on your computer doesn't have. Teams support is planned for Lumi Cloud.

## What applies

The gateway builds each chat's session the way [`lumi run`](headless.md)
does:

- file tools stay inside the project, and its [exclusions](enterprise-policy.md)
  apply;
- the guardrails and your organization's [policy](enterprise-policy.md)
  apply, including the modes and models it allows;
- budgets, [usage records](usage-and-costs.md) and the
  [audit log](audit-log.md) apply, with the chat recorded as
  `gateway:<chat id>`;
- your own [hooks](packs.md#hooks) in `settings.json` run, as in the app. A
  guard that refuses a call refuses it before anything is asked in the chat.
  The gateway reads them when it starts, so restart it after changing them;
- the saved key values are removed from approval requests before they're
  sent.

A project's own instructions, notes and `lumi-policy.json` allow rules apply
only if you trust the project in the app. The gateway never trusts a project
itself.

Anyone who can write in an allowed chat can use the agent on your computer.
Allow only chats you control, and prefer `ask` or `auto-edit` to `bypass`.
**Settings > Privacy & security > Chat gateway** turns the gateway off, and an
organization can lock it off with `security.chat_gateway` in its policy.
