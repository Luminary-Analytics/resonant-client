# Issue trackers: Jira, Linear, GitHub and GitLab

Start work from an issue, and link the result back to it.

- **Attach an issue** to your message: `@issue:ENG-12`, `@issue:#34`, or the
  issue's link. The agent gets the issue's title, state, assignee, labels,
  description and latest comments with your message.
- **Ask the agent** to read one: it uses `issue_view` with the same names.
- **Link back**: ask the agent to comment on the issue when it's done, for
  example with the pull request it opened. It uses `issue_comment`. Other
  people see the comment, so in Ask and Auto-edit modes Lumi asks you first.

## Naming an issue

| You write | Means |
| --- | --- |
| A link | `https://acme.atlassian.net/browse/ENG-12`, `https://linear.app/acme/issue/ENG-12/...`, `https://github.com/acme/app/issues/34`, `https://gitlab.com/group/app/-/issues/5` |
| `jira:ENG-12`, `linear:ENG-12` | That tracker's issue |
| `ENG-12` | The issue in Jira or Linear, whichever is set up (with both, add the prefix) |
| `#34` | Issue 34 of this project's GitHub or GitLab repository (its `origin`) |
| `github:acme/app#34`, `gitlab:group/app#5` | An issue in another repository |

## Setting up

Open **Settings > Issue trackers**.

- **Jira Cloud**: the site (`https://your-team.atlassian.net`), your
  account's email, and an [API token](https://id.atlassian.com/manage-profile/security/api-tokens).
- **Jira Server or Data Center**: the site, no email, and a personal access
  token.
- **Linear**: a personal API key from Linear's settings.
- **GitHub and GitLab issues** use the tokens you set for pull requests
  (Settings > Connections > API keys).

The environment variables `JIRA_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` and
`LINEAR_API_KEY` work too, for `lumi run` in CI. Tokens are kept in the
credential store, sent only in request headers, and removed from anything
sent to a model by the secret scan.

## What the agent sees

An issue is written by other people, so Lumi presents its description and
comments as the issue's content: information to consider, not instructions to
follow. Descriptions are shortened after 6,000 characters and comments after
1,500, and only the latest 10 comments are shown.

## Not yet

- Changing an issue's state, assignee or fields.
- Starting a session from the tracker itself: from a Jira or Linear button,
  or by assigning the issue to Lumi.
