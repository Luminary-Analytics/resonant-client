# Team skills, prompts and project notes

Your organization can publish **skills** and **prompts** in Lumi Cloud, under
**Library**, for everyone's Lumi. Each change to an item is a new version,
and Lumi gets the latest one. **Project notes** you share from Lumi reach
everyone working on the same repository once they're approved.

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

## Project notes for the team

A note in **Project notes** (the toolbar's document button) can go to
everyone working on the same repository.

1. **Sharing**: choose **Share with the team** on a note. Lumi proposes it in
   your organization's Lumi Cloud and keeps its provenance:
   - its text, kind and source;
   - the repository, from the project's origin remote (such as
     `github.com/acme/web`);
   - the content hash of each source file, with line endings made alike, so
     Windows and other checkouts agree.

   A note whose files changed since it was saved can't be shared until you
   review and save it again.
2. **Approving**: the people who publish to the library approve or reject it
   under **Library > Project notes**. They can archive it later.
3. **Recall**: in every clone of that repository, Lumi syncs approved notes
   with the library.
   - Project notes lists them under **From your team**, with who wrote and who
     approved each one.
   - A turn recalls up to six that bear on the request, constraints first.
   - A note whose files differ in this checkout isn't recalled.

Team notes come from your organization, not the repository, so they're
recalled whether or not the project is trusted. They're framed as reference
evidence: current instructions take precedence. Your own notes stay in the
project's `.lumi/memory.json`.
