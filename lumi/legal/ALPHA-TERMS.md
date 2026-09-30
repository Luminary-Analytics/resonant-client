<!-- Rendered by packaging/legal_texts.py from lumi/legal/templates/ALPHA-TERMS.md and lumi/legal/terms.json. Edit those, then run: python packaging/legal_texts.py render -->
# Lumi Alpha and Beta Test Terms

Version 1.0, published September 29, 2026.
This version applies to you from the day you accept it.

These Alpha and Beta Test Terms (the "Alpha Terms") supplement the Lumi End
User License Agreement (the "EULA") between you and Luminary Analytics, LLC
("Luminary," "we" or "us"). They apply to every Pre-Release Build of Lumi: a
build whose version number carries a pre-release label, such as
`0.21.0-alpha.1`, `0.21.0-beta.2`, `0.21.0-rc.1` or `0.20.0.dev0`, however
you received it. The label alone decides: a generally available release is
not a Pre-Release Build, even when it reaches you on the beta update channel.
Words defined in the EULA mean the same here. For Pre-Release Builds, where
these Alpha Terms and the EULA conflict, these Alpha Terms control.

You accept these Alpha Terms, together with the EULA, when you accept them in
Lumi or in its installer; when you type "yes" where Lumi's command-line tools
show them to you; when you run `lumi terms accept`, or use Lumi's
`--accept-terms` option or `LUMI_ACCEPT_TERMS` setting, naming this version;
or when you install or use a Pre-Release Build. An organization can accept
them for the people who use Lumi on its computers only through Lumi's machine
policy, as section 2.4 of the EULA describes, and is then bound by them for
those people.

## 1. Pre-release software, provided as is

Pre-Release Builds are unfinished software, offered for testing and
evaluation. They may contain errors, may stop working, may lose or corrupt
data, and may behave unexpectedly, including when the agent acts on your
computer. They may differ substantially from any generally available release.
PRE-RELEASE BUILDS ARE PROVIDED "AS IS" AND "AS AVAILABLE," WITH ALL FAULTS
AND WITHOUT WARRANTY OF ANY KIND, AND THE DISCLAIMER OF WARRANTIES AND THE
LIMITATION OF LIABILITY IN SECTIONS 12 AND 13 OF THE EULA APPLY TO THEM IN
FULL. Luminary has no obligation to support Pre-Release Builds, to correct
their errors or to respond to feedback about them.

## 2. Luminary may change or end the program

Luminary may change, suspend or end the alpha or beta program, any Pre-Release
Build, or any feature of one, at any time, with or without notice. A feature
in a Pre-Release Build may change or be removed, may never appear in a
generally available release, or may later be offered only as part of a paid
plan or service. Luminary has no obligation to make a generally available
release of Lumi.

## 3. Not for production-critical use

Don't use Pre-Release Builds for work where a failure, a mistake or lost data
would cause you or others significant harm. In particular:

- don't point a Pre-Release Build at production systems, production data or
  repositories you can't restore, and keep backups of everything it can
  reach;
- prefer permission modes that ask before acting, and don't give it
  credentials with broad access to production systems or sensitive data; and
- review its changes before you rely on them, as the EULA requires for all
  Output.

You are responsible for deciding whether a Pre-Release Build is suitable for
what you use it for.

## 4. No confidentiality obligation

The alpha is open to the public. You have no obligation to keep Pre-Release
Builds, their features or your experience with them confidential: you may
talk and write about them, and publish reviews, screenshots and benchmarks, as
long as you don't suggest that Luminary sponsors or endorses what you publish.
If you find a security vulnerability, we ask that you report it privately to
Luminary at rich.bellantoni@luminaryanalytics.com first, and give Luminary a reasonable time to
fix it before you publish its details. Likewise, Luminary has no obligation
to keep confidential what you send it about Pre-Release Builds, so don't send
confidential information.

## 5. Luminary may use your feedback freely

Everything you send Luminary about Pre-Release Builds, including feedback, bug
reports, suggestions and diagnostics, is Feedback under section 8 of the EULA.
Luminary may use, copy, modify, publish and otherwise exploit it for any
purpose, including to improve Lumi and Luminary's other products and
services, without restriction, payment or attribution.

## 6. Feedback and diagnostics only when you send them

Luminary receives feedback about Pre-Release Builds, and any diagnostics with
it, only after you choose to send it. The Privacy Notice describes what a
report contains, where it goes, and how your organization can limit it or
turn it off, as well as what else Lumi sends, and when. Review a report
before you send it: diagnostics can contain parts of your conversations.

## 7. How long the pre-release license lasts

Your license to use a Pre-Release Build under the EULA and these Alpha Terms
lasts until the earlier of:

- the end of the alpha or beta program the build belongs to, which Luminary
  will announce in Lumi, in its release notes or on its website; and
- the day Luminary makes a generally available (stable) release of Lumi that
  succeeds the build,

unless Luminary's announcement gives a later date, and unless it ends sooner
under the EULA. After that, stop using the Pre-Release Build. You may use
generally available releases under the EULA; Settings > Updates in Lumi can
switch you to the stable channel.

## 8. Leaving the alpha or beta

You may stop testing at any time:

1. To keep using Lumi without receiving more Pre-Release Builds, choose the
   stable channel in Settings > Updates. Lumi keeps the version you have and
   updates to the next stable release that is newer than it.
2. To leave entirely, uninstall Lumi, then delete the folder where it keeps
   its settings, conversations and records: `.lumi` in your home folder
   (`~/.lumi`; on Windows, `%USERPROFILE%\.lumi`).
3. If you saved API keys or signed in, also remove Lumi's entries from your
   operating system's credential store (Windows Credential Manager, the macOS
   Keychain or your Linux desktop's secret service): they are saved under the
   name "Lumi" (on Windows, named Lumi or ending in @Lumi).

The Privacy Notice lists the other places Lumi may have written, such as
`.lumi` folders in projects you opened. Deleting Lumi's local data doesn't
delete what your Model Providers, your organization's Lumi Cloud or Luminary
(for example, Feedback you sent) already hold.

## 9. Everything else

Except as these Alpha Terms say otherwise, the EULA applies to Pre-Release
Builds, including its sections on governing law and venue, notices and changes
to these terms. Send notices about these Alpha Terms to rich.bellantoni@luminaryanalytics.com.
