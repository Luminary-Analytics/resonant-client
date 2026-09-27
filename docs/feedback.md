# Sending feedback

**First pass, for the alpha.** Lumi's side is `lumi/feedback.py` and
`lumi/gui/static/feedback_view.js`. Lumi Cloud receives reports at
`POST /api/v1/feedback` and keeps them in a staff-only inbox.

## Opening it

**Send feedback** opens a dialog. You can reach it from:

- **Help › Send Feedback…** in the application menu (with the pointer);
- the command palette: **Ctrl+K**, then "feedback";
- **Send feedback…** in the profile menu (the account button at the bottom
  left, with the arrow keys);
- **Settings › About Lumi › Send feedback…**.

The dialog works from the keyboard:

- Tab and Shift+Tab stay inside it.
- Ctrl+Enter sends from the message.
- Escape closes it, and focus returns to what opened it. From the
  application menu, whose items can't take focus, it returns to the Menu
  button.

## What you fill in

| Field | Rule |
| --- | --- |
| Kind | Bug, Idea or Other |
| Message | Required, up to 5,000 characters. Control characters (apart from tabs and line breaks) and bidirectional controls, which can make text read differently than it's stored, are removed |
| Reply-to email | Optional, for an answer. One address, up to 254 characters, in ASCII (`name@example.com`; `+`, `.`, `'`, `%`, `_` and `-` allowed before the @) |
| Include diagnostics | Off by default |

## What is sent

Every report carries:

- the kind and the message;
- the reply-to address, if you gave one;
- the app's version, update channel (`stable` or `beta`), operating system
  and architecture;
- an install id, which lets staff see that several reports came from one
  install. It's derived (HMAC-SHA256) from a random secret made the first
  time and kept in `feedback/install-id` in Lumi's state folder, which
  comes from nothing on the computer or your account. Each Lumi Cloud gets
  its own id, and so do signed-out reports and each account's: the id your
  signed-in reports carry never matches the one your signed-out reports
  carry, so it can't tie them to you.

**With Include diagnostics** the report also carries:

- Python's version and the platform;
- whether this is a packaged build;
- the provider type and model in use (a custom connection is shown as
  `connection`; never a key);
- whether offline mode is on;
- the end of Lumi's startup log (packaged builds write it): its last 60
  whole lines, up to 6,000 characters.

The log goes through the diagnostics bundle's redaction
(`gui/diagnostics.py`), which removes saved key values and credential
formats, before it's cut to those lines, so a cut never leaves part of a
secret that no longer looks like one. Your home folder is written as `~`,
also where it's written with doubled backslashes (as in JSON). Terminal
escape codes and other control characters are removed. The rest of the
diagnostics bundle (session logs, settings and mission audits) is never part
of a report.

**You see the report before it's sent.** With diagnostics included, the
dialog shows the whole report, exactly as JSON, under **What Lumi will
send**, and says where it goes and whether as your account. Send sends that
very report: the app keeps the one it showed you (`preview_id`, for 15
minutes). If the form changes after that, or where the report would go or
as whom, the report is prepared again, and Send waits until you've seen the
new one. So log lines written after the preview never go.

## Before anything leaves the computer

1. **Offline mode** refuses the report unless the Lumi Cloud host is
   allowed, before anything else looks at it: the report isn't prepared,
   so diagnostics aren't gathered and the DLP rules don't check it. With
   offline mode on and no Lumi Cloud set up, it's refused, not kept to send
   later.
2. **The secret scan**, with its patterns on even when Settings › Privacy
   has it off, removes saved key values and anything shaped like a
   credential from the message and the diagnostics.
3. **Your organization's DLP rules** check what's left, when a policy has
   them ([DLP](dlp.md); purpose `feedback`: the message and reply-to as
   `prompt`, diagnostics as `attachment`).
   - A block refuses the report.
   - Redactions apply to what is sent.
   - A reply-to address the rules would change is left out.
   - A policy Lumi can't use refuses the report too.

A message that redaction markers make longer than 8,000 characters isn't
sent (shorten it); a diagnostic text keeps its last 12,000 characters.

A refusal says why and offers **Copy to clipboard**. The copy is the report
as text as it would have been sent: secrets removed and the DLP rules
applied (only in your clipboard; nothing is sent). When the DLP rules
refused the report, there's no copy either. A result that arrives after
you closed the dialog is shown as a notification.

## Where it goes

A report goes to the Lumi Cloud set in **Settings › Lumi account** (or by
your organization's policy), through Lumi's shared HTTP settings: proxy,
system certificates and offline mode (`net.client_options`). An address Lumi
wouldn't sign in to (plain `http` to another computer) counts as no address.

**Your account goes only while you're signed in** to that Lumi Cloud: the
one you signed in at. The request then carries your access token
(`Authorization: Bearer`), so staff see who sent it. When your
organization's policy has since named another Lumi Cloud, reports go there
without your account, and your token never goes to a host that didn't issue
it. Signed out, nothing ties the report to you. A report written while
signed out is never attributed later. A report written while signed in is
sent as you only while you are still the one signed in; if your sign-in
ended meanwhile, it goes without your account, and the record says so.

**When it can't go now, it waits on this computer:** with no Lumi Cloud
address, when Lumi Cloud can't be reached or answers with a server error or
408, when it's busy (429), when it asks for a sign-in (401 or 403, which the
contract never asks for), or when your access token can't be refreshed just
now.

- Up to 20 reports and 512 KB wait in `feedback/queue.json`, for 30 days.
- Lumi tries again in the background: a minute after it starts, then every
  minute for the reports that are due. A report that couldn't be sent waits
  10 minutes, then twice as long after each failed try, up to 6 hours, or as
  long as a 429's `Retry-After` says. A report waiting for an address is due
  at once, and setting the address or signing in wakes the background.
- A report goes only to the Lumi Cloud it was written for (one written
  with no address set goes to the first one set).
- One report Lumi Cloud can't take ends the round, so an unreachable server
  isn't asked once per waiting report.
- Each report is checked again before it's sent (the secret scan and DLP),
  since the rules may have changed.
- The dialog lists how many reports are waiting, with **Send now** (every
  waiting report, without waiting out the backoff; a 429's `Retry-After`
  is still waited out) and **Discard** (a report being sent at that moment
  can't be taken back; the dialog says so).
- A report Lumi Cloud refuses (a 3xx or a 4xx answer other than 401, 403,
  408 and 429) is dropped, and so is one DLP now blocks or one 30 days old.
  Redirects are never followed.

A report Lumi Cloud refuses while you send it isn't kept; the dialog says
why. **Five reports in ten minutes** is the most you can send (queued ones
count).

## The record

The [audit log](audit-log.md) records each report's kind and size in bytes,
and whether it had diagnostics; never its text, reply-to or install id:

| Type | When | Also |
| --- | --- | --- |
| `feedback.sent` | Lumi Cloud took a report | `queued` (sent from the waiting list), `attributed` (it went with your account) |
| `feedback.queued` | A report waits on this computer | `reason`: `no_cloud`, `unreachable`, `busy`, `unauthorized` |
| `feedback.refused` | A report wasn't sent or kept | `reason`: `offline`, `dlp`, `rate_limited`, `queue_full`, `refused` (with Lumi Cloud's `status`) |
| `feedback.dropped` | A waiting report was removed | `reason`: `refused`, `dlp`, `expired`, `discarded` |

## The contract with Lumi Cloud

`POST <Lumi Cloud>/api/v1/feedback`, with `Content-Type: application/json`:

```json
{"kind": "bug",
 "message": "The build button does nothing.",
 "reply_to": "ada@example.com",
 "app": {"version": "0.19.2", "channel": "stable", "os": "Windows 11", "arch": "amd64"},
 "install_id": "K0zBHJUDQFlgKRs0UDfFczS1CPc5Oo4V",
 "diagnostics": null}
```

| Field | Form |
| --- | --- |
| `kind` | `bug`, `idea` or `other` |
| `message` | 1 to 8,000 characters (the dialog takes 5,000; markers such as `[REDACTED GitHub token]` can make it longer); tabs and line breaks allowed, no other control characters and no bidirectional controls |
| `reply_to` | `null`, or an address matching `EMAIL` in `lumi/feedback.py` |
| `app.version` | `[0-9A-Za-z][0-9A-Za-z.+-]{0,39}` |
| `app.channel` | `stable` or `beta` (Lumi Cloud takes any short lower-case name) |
| `app.os` | Letters, digits, spaces and `._()-`, up to 60 characters |
| `app.arch` | Lower-case letters, digits and `_.-`, up to 20 characters |
| `install_id` | `[A-Za-z0-9_-]{16,64}` |
| `diagnostics` | `null`, or an object of the fields above: strings, numbers and booleans |

The optional header `Authorization: Bearer <desktop access token>` attributes
the report to the signed-in account. Answers:

| Status | Body | Lumi |
| --- | --- | --- |
| 201 | `{"id": "fbk_…"}` | Shows the id as the report's reference |
| 400 | `{"error": "invalid_request", "error_description": "…"}` | Says why and doesn't keep the report |
| 413 | `{"error": "too_large", …}` | Says so; shorten it or leave diagnostics out |
| 429 | `{"error": "slow_down", …}` and `Retry-After` in seconds | Keeps it and tries again after that |
| 5xx, 408, no answer | | Keeps it and tries again later |
| 401, 403 | | Keeps it and tries again later (the contract never asks for a sign-in) |
| 404 | | This Lumi Cloud doesn't take feedback: says so |
| 3xx, other 4xx | | Says so and doesn't keep the report; redirects aren't followed |

## Not built yet

- **No default Lumi Cloud.** Without an address in Settings › Lumi
  account, a report waits on this computer until one is set. Whether the
  alpha should ship a feedback address of its own is still open.
- **Attachments and screenshots.** A report is text.
- **Answers in the app.** Staff reply by email, to the reply-to address.
- **Keyboard access to the application menu.** Its items take the pointer;
  the command palette, the profile menu and About are the keyboard's ways in.
