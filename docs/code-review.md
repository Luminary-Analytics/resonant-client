# Agent changes wait for a reviewer

For teams where every change needs a human reviewer, **Settings > Code
review > Agent changes wait for a reviewer** keeps the agent from shipping
its own work. An organization can turn it on for everyone, and name the
reviewers, in its Lumi Cloud policy ("Agent changes wait for a reviewer");
Settings then shows it locked.

While it's on:

- **The agent doesn't merge or push to a default branch.** These are refused
  in every permission mode, before any allow rule of a repository or
  organization:
  - `gh pr merge` and `glab mr merge`;
  - completing an Azure DevOps pull request;
  - `git push` naming `main`, `master`, `trunk` or `production`.

  The agent is told who reviews instead. Like Lumi's other guardrails, this
  catches the command as written, not one built to hide.
- **Its pull requests name the reviewers.** When the agent opens a pull
  request with `github_pr_create`:
  - The description ends with "This change was made by an AI agent (Lumi).
    It needs review by @octocat before it merges."
  - On GitHub, Lumi also requests review from the people and teams under
    **Reviewers** (GitHub usernames, or `organization/team`). On GitLab,
    Bitbucket and Azure DevOps, the description names them.
- **Your organization sees what's waiting.** When you're signed in to Lumi
  Cloud, the pull request joins the organization's **Review queue** there,
  with its reviewers.
  - `github_pr_view` reports its state as reviews come in: waiting, changes
    requested, approved, merged or closed.
  - The queue uses the organization this computer is enrolled in, or else
    your first organization.

Merging stays with people. Use your forge's branch protection (required
reviews on the default branch) to make the approval a condition of merging;
Lumi's part is that its agent never merges or pushes past it.
