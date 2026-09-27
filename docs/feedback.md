# Sending feedback

**First pass, for the alpha.** Lumi's side is `lumi/feedback.py` and
`lumi/gui/static/feedback_view.js`. The receiving side is a Lumi Cloud's
`POST /api/v1/feedback`, which keeps reports in a staff-only inbox.

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
- A refusal from the last time it was open is gone when it opens again; what
  you typed stays.
- An outcome that arrives after you closed it is announced to screen readers
  (a polite live region) as well as shown for a moment.

## What you fill in

| Field | Rule |
| --- | --- |
| Kind | Bug, Idea or Other |
| Message | Required, up to 5,000 characters as sent: control characters (apart from tabs and line breaks) and bidirectional controls, which can make text read differently than it's stored, are removed, and so is space at either end. An emoji counts as one character. The dialog counts the same way |
| Reply-to email | Optional, for an answer. One address, up to 254 characters, in ASCII (`name@example.com`; `+`, `.`, `'`, `%`, `_` and `-` allowed before the @) |
| Include diagnostics | Off by default, and not offered when your organization doesn't allow it |

## Where it goes

Reports go to the **feedback address**:

1. `privacy.feedback_url`, which your organization's policy can lock, or you
   can set in `settings.json`;
2. otherwise the address this build of Lumi was made with
   (`feedback.BUILD_DESTINATION`: none yet; alpha builds will send to
   Luminary's Lumi Cloud once it exists);
3. otherwise the Lumi Cloud this computer uses (**Settings › Lumi account**,
   or your organization's policy).

The dialog says where a report goes before you send it, and who reads it
there when that Lumi Cloud says so (`GET /api/v1/feedback/info`, asked only
when there is an address, offline mode allows it and feedback isn't turned
off). An address with a user name or password in it isn't used.

**A report is bound to its address when it's written.** It goes there and
nowhere else, even if the address changes later. A report written while no
address was set stays on this computer: once there is one, the dialog lists
it with **Send to** that address, and it goes only when you choose that.
It never goes by itself to an address that appears later, such as one an
employer's policy sets.

**Your account goes only while you're signed in** to the Lumi Cloud that
issued your sign-in, and only when reports go there. The request then
carries your access token (`Authorization: Bearer`), so staff see who sent
it; your token never goes to a host that didn't issue it. A report goes as
you are when it's sent: written while signed in and sent after you signed
out, it goes without your account, and its install id is the one for
reports sent signed out.

## What is sent

Every report carries:

- the kind and the message;
- the reply-to address, if you gave one;
- the app's version, update channel (`stable` or `beta`), operating system
  and architecture;
- an install id, which lets staff see that several reports came from one
  install. It's derived (HMAC-SHA256) from a random secret made the first
  time and kept in `feedback/install-id` in Lumi's state folder, which comes
  from nothing on the computer or your account. Each address gets its own
  id, and so do signed-out reports and each account's: the id your
  signed-in reports carry never matches the one your signed-out reports
  carry.

**With Include diagnostics** the report also carries:

- Python's version and the platform;
- whether this is a packaged build;
- the provider type and model in use (a custom connection is shown as
  `connection`; never a key);
- whether offline mode is on;
- the end of Lumi's startup log (packaged builds write it): its last 60
  whole lines, up to 6,000 characters. The log can hold parts of recent
  conversations (warnings quote model output) and file names, and the
  dialog says so.

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
very report (`preview_id`, kept 15 minutes), checked again first. If the
form changes after that, or where the report would go or as whom, the report
is prepared again, and Send waits until you've seen the new one. So log
lines written after the preview never go.

## Before anything leaves the computer

1. **Your organization's switch.** With `privacy.feedback` off, nothing is
   sent and the dialog says so; with `privacy.feedback_diagnostics` set to
   `never`, reports can't include diagnostics.
2. **Offline mode** refuses the report unless the address is allowed,
   before anything else looks at it: the report isn't prepared, so
   diagnostics aren't gathered and the DLP rules don't check it. With
   offline mode on and no address set, it's refused, not kept to send later.
3. **The secret scan**, with its patterns on even when Settings › Privacy
   has it off, removes saved key values and anything shaped like a
   credential from the message and the diagnostics. A reply-to address that
   looks like a secret is left out.
4. **Your organization's DLP rules** check what's left, when a policy has
   them ([DLP](dlp.md); purpose `feedback`: the message and reply-to as
   `prompt`, diagnostics as mixed content, which every rule checks whatever
   its scope).
   - A block refuses the report.
   - Redactions apply to what is sent.
   - A reply-to address the rules would change is left out.
   - A policy Lumi can't use refuses the report too.
   - While you type, the report shown is checked with the rules on this
     computer only. The organization's DLP service (`dlp.service`) sees a
     report only when you send it; if it changes the report, the dialog shows
     it as it is now, and it goes only when you choose Send again. The same
     happens when the rules changed since you reviewed it.
5. **Lumi Cloud's limits**, enforced before sending: a message of up to
   8,000 characters as sent (markers such as `[REDACTED GitHub token]` can
   make it longer than you typed; a longer one isn't sent), each diagnostic
   text up to 16,000 characters, the diagnostics up to 32 KB and the whole
   report up to 64 KB, in UTF-8. When the diagnostics are too large, the
   log's oldest lines are left out, and the dialog says so.

**The outcome says what the checks changed**: secrets removed, a
redaction, a reply-to address left out, log lines left out. It's shown with
the report you review, and again with the result.

A refusal says why and, when there's something to give, offers **Copy to
clipboard**:

- after an offline refusal, only what you typed (kind, message and reply-to,
  with saved keys and secret-looking values taken out): no diagnostics and no
  DLP check, since nothing was prepared;
- when Lumi Cloud or the rate limit refuses it, the report as it would have
  been sent;
- nothing when the DLP rules refused it.

## When it can't go now

**It waits on this computer** in `feedback/queue.json` (up to 20 reports and
512 KB), under a lock every Lumi process on this computer takes, so the app
and the terminal UI can't lose one another's reports:

| Waiting because | What happens |
| --- | --- |
| No address was set when it was written | Kept until you send it to the address shown, or discard it |
| Lumi Cloud couldn't be reached, answered 5xx or 408, or didn't acknowledge it (a captive portal's page, a 202 or 204) | Tried again: after 10 minutes, then twice as long after each try, up to 6 hours |
| 429 | After `Retry-After` (whole seconds, at most an hour, with a little jitter) |
| Your token was refused even after one refresh (401), or Lumi Cloud asked for a sign-in (401 or 403 with no token) | When you sign in again (or **Send now**); never sent without your account instead |
| Your access token couldn't be refreshed just now | Tried again as above |
| Lumi Cloud doesn't accept feedback (404) | Kept, with **Copy** and **Discard**; **Send now** tries again |
| Lumi Cloud refused it (400, 413 or another 4xx) | Kept as couldn't be delivered, with the reason, **Copy** and **Discard** |
| The DLP rules now block it | Kept, with **Discard** (no copy) |
| It waited 30 days | Kept as expired, with **Copy** and **Discard**; not sent any more |

- Backoff and `Retry-After` run on the clock that only moves forward, within
  one run of Lumi: moving the computer's clock back doesn't strand a report,
  and after a restart every waiting report is tried at the first round.
- The 30 days count time Lumi was running, each round adding at most ten
  minutes, so moving the clock forward (or a computer asleep) doesn't expire
  the queue.
- Each report is checked again before it's sent (the secret scan and DLP),
  since the rules may have changed.
- One report Lumi Cloud can't take now ends the round, so an unreachable
  server isn't asked once per waiting report.
- The dialog lists the waiting reports: how many wait for the address shown,
  with **Send now** and **Discard all**, and each other one with what it
  waits for (another address, a sign-in, you) and its own **Copy**, **Send
  to** or **Discard**. A report being sent at that moment can't be taken back;
  the dialog says so.
- A damaged entry in the file is dropped (and recorded), never stops the
  others; a file that isn't JSON is set aside.

When Lumi Cloud refuses a report you're sending (400, 413 or 404), the
dialog says why and the form keeps it, with **Copy**. **Five reports in ten
minutes** is the most you can send (kept ones count).

## The record

The [audit log](audit-log.md) records each report's kind and size in bytes,
and whether it had diagnostics; never its text, reply-to or install id:

| Type | When | Also |
| --- | --- | --- |
| `feedback.sent` | Lumi Cloud acknowledged a report | `queued` (sent from the waiting list), `attributed` (Lumi Cloud says it went with your account) |
| `feedback.queued` | A report waits to be tried again | `reason`: `unreachable`, `busy`, `unconfirmed`, `unauthorized`, `sign_in` |
| `feedback.held` | A report waits for you | `reason`: `no_destination`, `not_accepting`, `too_large`, `refused` (with Lumi Cloud's `status`), `dlp`, `expired` |
| `feedback.refused` | A report wasn't sent or kept | `reason`: `offline`, `dlp`, `disabled`, `rate_limited`, `queue_full`, `refused`, `too_large`, `not_accepting` (with Lumi Cloud's `status`) |
| `feedback.dropped` | A waiting report was removed | `reason`: `discarded`, `damaged` |

## The contract with Lumi Cloud

`POST <address>/api/v1/feedback`, with `Content-Type: application/json` and
`Idempotency-Key: <the report's id>`, a UUID made when the report is written
and the same for every try:

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
| `message` | 1 to 8,000 characters as sent; tabs and line breaks allowed, no other control characters and no bidirectional controls |
| `reply_to` | `null`, or an address matching `EMAIL` in `lumi/feedback.py` |
| `app.version` | `[0-9A-Za-z][0-9A-Za-z.+-]{0,39}` |
| `app.channel` | `stable` or `beta` (Lumi Cloud takes any short lower-case name) |
| `app.os` | Letters, digits, spaces and `._()-`, up to 60 characters |
| `app.arch` | Lower-case letters, digits and `_.-`, up to 20 characters |
| `install_id` | `[A-Za-z0-9_-]{16,64}` |
| `diagnostics` | `null`, or an object of the fields above: strings (up to 16,000 characters each), numbers and booleans; at most 32 KB as JSON |

The key is a header, not a field: a Lumi Cloud refuses fields it doesn't
know, and an older one must not refuse a newer app. The optional header
`Authorization: Bearer <desktop access token>` attributes the report to the
signed-in account. Answers:

| Status | Body | Lumi |
| --- | --- | --- |
| 201 | `{"id": "fbk_…", "report": "<the key>", "account": true}` | Delivered: shows the id as the report's reference |
| 200 | The same, for a report Lumi Cloud already has (a retry of the same key from the same install) | Delivered |
| 200 or 201 whose `report` isn't the key, or no JSON | | Not delivered (a captive portal, say): kept and tried again |
| 401 `{"error": "invalid_token"}` with `WWW-Authenticate: Bearer error="invalid_token"` | | The token presented was refused: refreshed once where it was issued and tried again; then kept for a new sign-in |
| 400 | `{"error": "invalid_request", "error_description": "…"}` | Says why; a waiting report is kept as couldn't be delivered |
| 413 | `{"error": "too_large", …}` | The same |
| 404 | | This Lumi Cloud doesn't accept feedback: says so, keeps it |
| 429 | `{"error": "slow_down", …}` and `Retry-After` in whole seconds | Keeps it and tries again after that (at most an hour) |
| 5xx, 408, no answer | | Keeps it and tries again later |
| 3xx, 202, 204 | | Not an acknowledgment: keeps it and tries again (redirects aren't followed) |

`GET <address>/api/v1/feedback/info` answers `{"accepting": true, "operator":
"<who reads the reports>"}`; the dialog shows "read by" that name next to
the address.

## For administrators

Lock these in the policy's `settings` ([organization policy](enterprise-policy.md)):

| Setting | Values |
| --- | --- |
| `privacy.feedback` | `"on"` (default) or `"off"`: whether people can send feedback from Lumi at all |
| `privacy.feedback_diagnostics` | `"allowed"` (default) or `"never"` |
| `privacy.feedback_url` | `""` (the build's address, else the Lumi Cloud the computer uses) or a Lumi Cloud address (`https`, no user name or password), such as your own Lumi Cloud's inbox |

## Not built yet

- **No feedback address in this build.** Without one in settings, a policy
  or the build, reports go to the Lumi Cloud this computer uses, and with no
  Lumi Cloud either, they wait until you send them somewhere.
- **Attachments and screenshots.** A report is text.
- **Answers in the app.** Staff reply by email, to the reply-to address.
- **Keyboard access to the application menu.** Its items take the pointer;
  the command palette, the profile menu and About are the keyboard's ways in.
