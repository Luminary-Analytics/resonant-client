# Lumi Cloud: your account and your organization

Lumi Cloud is where an organization manages Lumi: its members, their seats,
the computers running Lumi, and the policy those computers enforce. Lumi
works fully without it. This page covers the app's side, in
**Settings > Lumi account**. Code: `lumi/cloud.py`.

## Signing in

1. Open **Settings > Lumi account** and enter your organization's Lumi Cloud
   address (for example `https://cloud.example.com`). If your organization's
   policy names it, the field is filled in and locked.
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
sign-in. **Sign out** ends it on Lumi Cloud too. Your organization can also
sign the app out from Lumi Cloud.

## Using your organization on this computer

Next to an organization where you have a seat, choose **Use on this
computer**. Lumi generates a key pair for this computer:

- the private key stays in the credential store;
- Lumi Cloud gets the public key and lists the computer on the organization's
  Devices page.

From then on the computer checks in about once an hour, even after you sign
out. **Leave on this computer** undoes it.

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
When the agent wants to do something the mode doesn't allow, Lumi asks you in
the chat with **Approve** and **Deny** buttons. No answer within 10 minutes,
or **stop** in the chat, refuses it. The reply goes back to the chat.

Only your own computer takes your requests: never a managed computer, and
never a colleague's. Your organization can turn this off with
`cloud.remote_tasks` in its policy.

## Sharing a conversation

To show a colleague how you got somewhere, right-click a conversation in the
sidebar (or use its **⋯** button) and choose **Share…**. Sign in to your
organization's Lumi Cloud first; you don't need to enroll this computer.

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

**Share…** shows the link again later, with **Copy link** and **Stop
sharing**. Stopping makes the link show nothing. In Lumi Cloud, **Shared
sessions** lists what you've shared, and an organization's owners and admins
see and can stop everything shared in it.

## Handing off work

**Hand off…** in a conversation's menu passes the conversation, your note and
where the work is (repository, branch and commit) to someone in your
organization. Lumi Cloud emails them, and their Lumi lists it under
**Hand-offs for you**. See [hand-offs](hand-offs.md).

## For administrators: enrolling managed computers

To enroll computers without anyone signing in:

1. Create an **enrollment token** on the Devices page in Lumi Cloud.
2. Deploy the machine policy that the page prints, through Group Policy,
   Intune, a configuration profile or `%ProgramData%\Lumi\policy.json`
   ([Deploying on Windows](deploy-windows.md)):

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
- Only keys in `trusted_keys`, the `PolicyKeys` registry value or
  `policy-keys.json` can sign the cloud policy. The file in `~/.lumi/cloud/`
  is only a cache.
- People can't leave a managed enrollment or point Lumi at a different Lumi
  Cloud.

Revoke an enrollment token once the rollout is done. Computers already
enrolled keep working until they are revoked on the Devices page.
