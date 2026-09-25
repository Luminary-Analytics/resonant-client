# Team skills and prompts

Your organization can publish **skills** and **prompts** in Lumi Cloud, under
**Library**, for everyone's Lumi. Each change to an item is a new version,
and Lumi gets the latest one.

- A **skill** is a procedure the agent follows when a request matches it,
  such as how your team cuts a release. It has a description and **Offer it
  for** words, such as `release, changelog`.
- A **prompt** is text you insert into a message, such as a review
  checklist.

Owners and admins publish by default; they can let every member publish. The
library keeps each item's history: an earlier version can be published
again, and archiving an item takes it out of everyone's Lumi.

## In Lumi

Sign in to your organization's Lumi Cloud under **Settings > Lumi account**.

- **Syncing**: Lumi syncs the library when it starts, if its copy is more
  than 15 minutes old. **Sync now** under **Settings > Lumi account > Team
  library** syncs at once and shows what's there. The copy lives in Lumi's
  state folder (`team/library.json`), so skills and prompts work offline.
  Signing out deletes it.
- **Skills**: when a request matches a team skill's name, description or
  **Offer it for** words, the agent sees it listed with its version. It
  reads the steps with `skill_view` (`team:<organization>/<name>`) before
  following them. Up to four team skills are listed for a request.
- **Prompts**: the **❝** button beside the message box appears once your
  organization has prompts.
  - Type to find one, then choose it (or press Enter for the first match).
  - Lumi adds it to your message, after anything you've already typed.
  - Nothing is sent until you send it.

Team skills are offered in conversations in the app. `lumi run`, scheduled
tasks and chat requests don't list skills, team ones included.
