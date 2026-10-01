# Lumi Cloud: your account and your organization

Lumi Cloud is where your Lumi account lives: a free personal workspace for
someone working alone, and for an organization its members, their seats, the
computers running Lumi, and the policy those computers enforce. Lumi works
fully without it. This page covers the app's side: the first launch, the
profile menu and **Settings > Lumi account**. Code: `lumi/cloud.py`.

## At first launch

After you accept Lumi's terms, Lumi offers once to sign in to Lumi Cloud:

- **Continue with Google**, **Continue with Microsoft** or **Continue with
  email** opens Luminary's Lumi Cloud, `https://cloud.lumi.luminaryanalytics.com`,
  in your browser, straight at that way of signing in (Lumi adds
  `provider=google|microsoft|email` to the sign-in address; a Lumi Cloud that
  doesn't offer it shows its usual page). Signing in there for the first time
  creates your free account.
- **Continue without an account** (or Escape) closes it for good. Every tool,
  provider and feature works without an account. You can sign in later from
  the profile menu (**Sign in to Lumi Cloud**) or Settings > Lumi account.

Lumi doesn't offer it in offline mode, on a computer whose machine policy
names a Lumi Cloud (the organization enrolls it), or once you've signed in or
chosen to continue (`onboarding.cloud_prompted` in `settings.json`).

**Your personal workspace.** When someone who isn't in any organization
signs in, Lumi Cloud makes them a free one-person workspace, and when it's
their only organization Lumi uses it on this computer straight away (as
**Use on this computer** below does). Inviting someone to it later makes it
a team. If setting up the computer fails, you stay signed in and Lumi says
so; choose **Use on this computer** in Settings > Lumi account to try again.
Someone already in an organization chooses which one this computer uses.

Once you're signed in, the profile corner shows your name and email, and the
profile menu's **Lumi account** opens Settings > Lumi account.

**Which Lumi Cloud.** A machine policy's address comes first, then the one
saved in Settings (your last sign-in's), then `LUMI_CLOUD_URL` when it's
set (for example `http://127.0.0.1:8700` when developing against a local
Lumi Cloud), then Luminary's.

## Signing in

1. Open **Settings > Lumi account**. The address is Luminary's Lumi Cloud
   unless you change it to your organization's (for example
   `https://cloud.example.com`). If your organization's policy names it, the
   field is filled in and locked.
2. Choose **Sign in with your browser**. Lumi opens your browser. Sign in there
   with your email, GitHub, Google or your company's single sign-on, and
   approve "Sign in to Lumi on this computer".
3. The browser says you're signed in, and the page lists your
   organizations, your role in each and whether you have a seat.

Behind this is the standard sign-in for desktop apps (OAuth 2.0 for native
apps, RFC 8252):

- The browser returns to a listener that Lumi opens on `127.0.0.1` only for
  this sign-in, and the exchange uses a PKCE code verifier.
- Lumi keeps the long-lived sign-in token in your system credential store,
  never in `settings.json`, and keeps the short-lived one only in memory.

This account is separate from your display name and from any SONN or ChatGPT
sign-in. **Sign out** ends it on this computer at once, and on Lumi Cloud too.
When Lumi Cloud can't be told (offline mode doesn't allow it, or it can't be
reached), Settings says so: that sign-in stays valid there until it expires,
and nothing of it is kept here. Your organization can also sign the app out
from Lumi Cloud. Signing in again, at the same Lumi Cloud or another, ends the
earlier sign-in where it was issued.

**Your sign-in belongs to the Lumi Cloud that issued it.** Lumi records its
address when the sign-in completes (a sign-in you cancel changes nothing),
and refreshes it, uses it and signs it out only there. If this computer later
uses another Lumi Cloud (your organization's policy enrolls it somewhere
else, or the address changes), you count as signed out for the new one:
nothing of your sign-in is sent to it, and Settings > Lumi account says
you're still signed in to the first one, with **Sign out of** it. An address
with a user name or password in it (`https://name:secret@…`) isn't accepted.
Addresses are compared as addresses: a capital letter, the default port
(`:443`), a trailing slash or an internationalized name written either way
don't make two of them different Lumi Clouds; any other part does.

Lumi reads the sign-in's address and its tokens together, and sends a token
only to the address it came with, never to one it reads again afterwards.
When a sign-in completes (or you sign out) while Lumi is refreshing the
earlier one, the refreshed tokens aren't kept: the one Lumi Cloud just made
is revoked where it was made.

## Using your organization on this computer

Next to an organization where you have a seat, choose **Use on this
computer**. Lumi generates a key pair for this computer:

- the private key stays in the credential store;
- Lumi Cloud gets the public key and lists the computer on the organization's
  Devices page.

From then on the computer checks in about once an hour (as often as every
five minutes if the organization's Lumi Cloud asks), even after you sign
out. **Leave on this computer** undoes it.

**The enrollment belongs to the Lumi Cloud it was made with.** Lumi records
its address when the computer enrolls, and sends every device request only
there: device tokens, check-ins, policy downloads, leaving, tasks from
Slack and Teams, and oversight's records and confirmations. If you later sign
in to another Lumi Cloud, this computer counts as enrolled elsewhere:
check-ins and the organization's policy still come from where it enrolled,
tasks from chat wait, and Settings > Lumi account says so, with **Leave …
on this computer**. Nothing of the enrollment goes to the other Lumi Cloud.

A check-in sends:

- the Lumi version and the operating system;
- the organization policy version in force;
- usage totals per model since the last check-in: requests, input and output
  tokens, and cost;
- turn counts since the last check-in (`lumi/activity.py`): how many turns
  finished, ended in an error or were stopped, how many had a passing check
  (`check_run`), how many files they changed, and how many times the app
  crashed.

A check-in never sends prompts, responses, code, file names, project paths or
session titles. Its answer can carry the organization's shared model credit
and the month's spend so far; Lumi stops model requests when it's used up
([shared credit](usage-and-costs.md#an-organizations-shared-credit)).

An organization's policy can also turn on **organization oversight**: each
turn's activity, optionally your messages and sessions' titles (secrets
removed), and security flags, sent separately from check-ins. Lumi then
shows a notice beside the message box naming the organization and what it
receives, sends nothing to a model until you confirm it with **I've read
this** (a record signed with this computer's device key goes to
`/api/v1/oversight/acknowledgments`), and lists exactly what is shared in
**Settings > Privacy & security**. Leaving the organization or signing out
forgets the confirmation. See [organization
oversight](organization-oversight.md).

### The organization's policy

When the organization publishes a policy, the next check-in downloads it:

- **Verified before use.** The policy is signed with the organization's key.
  Lumi applies it only if the signature matches the key it pinned when you
  joined (or keys your administrator set, below).
- **What it can do.** It limits models, permission modes and settings, as a
  machine policy does ([Organization policy](enterprise-policy.md)).
  Settings show which organization manages a value.
- **Expiry.** A downloaded policy lasts 14 days and is refreshed well before
  then. If this computer can't reach Lumi Cloud, Lumi keeps enforcing the
  policy for a 7-day grace period, then refuses model requests until it
  checks in again.
- **Revocation.** If an administrator revokes this computer or removes you
  from the organization, the next check-in removes the enrollment and the
  downloaded policy.

An organization you join yourself doesn't override anything your computer's
administrator set. With a machine policy in place, its rules apply instead.

## Tasks from Slack and Teams

When your organization connects Slack or Microsoft Teams in Lumi Cloud, you
can message Lumi there and have one of your own computers do the work. On
this computer, open **Settings > Lumi account**, turn on **Tasks from Slack and
Teams**, and choose:

- **Project folder**: where requests run. Pick a project you aren't editing
  at the same time, or a separate checkout.
- **Permission mode**: **Ask before changes** (the default), **Edit files,
  ask about the rest**, or **Ask about nothing**.

While Lumi is open, it checks for your requests every 20 seconds and runs
them one at a time, with your default model and a session built like
[`lumi run`](headless.md)'s. The project's exclusions and your organization's
policy apply, and the project's own instructions apply only if you trust it.
Your own [hooks](packs.md#hooks) in `settings.json` run as they do in the
app; a guard that refuses a call refuses it before anything is asked in the
chat.
When the agent wants to do something the mode doesn't allow, Lumi asks you in
the chat with **Approve** and **Deny** buttons. No answer within 10 minutes,
or **stop** in the chat, refuses it. The reply goes back to the chat.

Only your own computer takes your requests: never a managed computer, and
never a colleague's. Requests wait while this computer uses another Lumi Cloud
than the one it enrolled with, and everything about a request (its approvals,
whether you said stop, its reply) goes back to the Lumi Cloud that handed it
out, or nowhere. Your organization can turn this off with
`cloud.remote_tasks` in its policy.

## Sharing a conversation

To show a colleague how you got somewhere, right-click a conversation in the
sidebar (or use its **⋯** button) and choose **Share in Lumi Cloud…**. Sign in
to your organization's Lumi Cloud first; you don't need to enroll this
computer. Without an account the dialog says that sharing needs one, and
that everything else in Lumi works without it; a teammate hand-off says the
same, while the CI hand-off works without an account. **Settings > Lumi
account** says who it's for before anyone signs in. The menu names Lumi Cloud
instead of hiding the item, so the same menus and documentation hold on every
computer, and the dialog explains what an account adds.

Lumi Cloud keeps a read-only copy at a link:

- **What's in it**: your messages, Lumi's replies, and a line for each action
  ("Read src/auth.py", "Ran `pytest -q`"), marked when it failed. It never
  holds what tools returned: file contents, command output and pages stay on
  this computer. Saved keys, and anything that looks like a token or
  password, are removed first. The project appears by its folder's name
  only.
- **Who can open it**: people in your organization, after signing in to
  Lumi Cloud (the default). An owner or admin can also let people share
  with **anyone who has the link**, under **Shared sessions** in Lumi Cloud;
  turning that off again closes those links.
- **It doesn't change** when the conversation does. To share a newer
  version, stop sharing and share again.

**Share in Lumi Cloud…** shows the link again later, with **Copy link** and **Stop
sharing**. Stopping makes the link show nothing. In Lumi Cloud, **Shared
sessions** lists what you've shared, and an organization's owners and admins
see and can stop everything shared in it.

## Handing off work

**Hand off…** in a conversation's menu passes the conversation, your note and
where the work is (repository, branch and commit) to someone in your
organization. Lumi Cloud emails them, and their Lumi lists it under
**Hand-offs for you**. See [hand-offs](hand-offs.md).

## Team skills and prompts

Skills and prompts your organization publishes in Lumi Cloud's **Library**
reach your Lumi: matching skills are offered to the agent, and the **❝**
button beside the message box inserts prompts. **Team library** on this page
shows what's synced, with **Sync now**. A synced copy belongs to the sign-in
it came with: after a sign-in at another Lumi Cloud, or as someone else, it
isn't offered until the next sync replaces it. See [team
library](team-library.md).

## Sending feedback

**Help › Send Feedback…** (also in the command palette, the profile menu and
About Lumi) sends a bug report, an idea or other feedback to a feedback
inbox, which only its staff can read: the address your organization or
this build of Lumi sets, else this Lumi Cloud's. You needn't sign in; while
you're signed in to the Lumi Cloud the report goes to, it carries your
account so staff see who sent it, and a report written so goes only with your
account: if you sign out before it goes, it waits for you, unless you choose
**Send without your account**. When it can't be reached, the report waits
on this computer and goes later; one written before any address was set goes
only when you send it to the address shown, as the account its button names. What a report holds, what never
leaves and how it's checked first: [Sending feedback](feedback.md).

## For administrators: enrolling managed computers

To enroll computers without anyone signing in:

1. Create an **enrollment token** on the Devices page in Lumi Cloud.
2. Deploy the machine policy that the page prints, through Group Policy,
   Intune, a configuration profile or `%ProgramData%\Lumi\policy.json` in a
   folder only administrators can change
   ([Deploying on Windows](deploy-windows.md),
   [the file rules](enterprise-policy.md#only-files-only-administrators-can-change-count)):

```json
{
  "schema": "lumi.policy/v1",
  "organization": "Acme",
  "cloud": {
    "url": "https://cloud.example.com",
    "organization_id": "org_…",
    "enrollment_token": "lce_…"
  },
  "trusted_keys": {"acme-20260925-1a90d1": "<base64 Ed25519 public key>"},
  "models": {"allowed": ["anthropic:*"]}
}
```

Lumi enrolls on its next start. After that:

- The published cloud policy **replaces** the rules in this machine policy.
- The machine policy's own rules (`models` above) apply until the first
  download, and whenever a download fails to verify.
- Only keys in `trusted_keys`, the `PolicyKeys` registry value (or
  configuration profile key), or on macOS and Linux a root-owned
  `policy-keys.json` beside the machine policy can sign the cloud policy.
  Windows doesn't read a `policy-keys.json`. The file in `~/.lumi/cloud/` is
  only a cache.
- People can't leave a managed enrollment or point Lumi at a different Lumi
  Cloud.
- **The machine policy's address is authoritative for enrollment.** When it
  names another Lumi Cloud than the one this computer enrolled with (you
  moved the computer to another deployment, or it had joined an organization
  itself), the old enrollment ends: the Lumi Cloud it was made with is told
  the computer left, with that computer's own device token and nowhere else,
  and its downloaded policy is deleted. With an `enrollment_token` for the
  new address, the computer then enrolls there; without one, it stays
  unenrolled. The old Lumi Cloud's device token is never sent to the new one.

Revoke an enrollment token once the rollout is done. Computers already
enrolled keep working until they are revoked on the Devices page.
