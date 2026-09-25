# A second person approves risky commands

Your organization can require a second person to approve some commands
before Lumi's agent runs them, such as force-pushes, production deploys or
`terraform apply`. It lists them in its Lumi Cloud policy, under **Commands a
second person approves before they run**. Each line is a pattern over the
whole command, with `*` for anything:

```text
git push --force*
terraform apply*
kubectl * --context production*
```

## What happens

1. **You allow it first.** When the agent wants to run a listed command, the
   usual permission rules come first: you allow it, or your permission mode
   does.
2. **Lumi asks Lumi Cloud.** A note in the conversation says the command is
   waiting for a second person in your organization, and who can approve it.
   - Lumi Cloud emails those people and lists the request on its
     **Approvals** page.
   - Saved keys and anything that looks like a secret are removed from the
     command first.
3. **Someone else answers.**
   - An approver other than you approves or denies it. By default that's an
     owner, admin or security admin; an owner or admin can let anyone else in
     the organization approve.
   - While Lumi waits (up to the policy's time, 30 minutes unless it says
     otherwise), **Stop** ends the wait.
4. **It runs only if it's approved.**
   - A denial, no answer in time, a stop, or no Lumi Cloud sign-in to ask
     through, and it doesn't run.
   - The agent is told why and asked to tell you.

Lumi doesn't ask anyone to approve what it never runs anyway: its
guardrails and the irreversibility floor (a force-push to `main`, say) still
refuse those first.

Sign in to your organization's Lumi Cloud under **Settings > Lumi account**.
The request goes to the organization this computer is enrolled in, or else
your first organization.
