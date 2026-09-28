# Plans

> Draft for product review. Paid plans and prices aren't decided.

## Free for individuals (today)

Everything in the Lumi app is free, and there is no account:

- the agent with every tool, provider and feature in the app;
- your own API keys (Anthropic, OpenAI, OpenRouter, custom connections), your
  ChatGPT sign-in for Codex, or models on your own computer (Ollama, EXO);
- local settings, sessions, notes, audit log and usage records.

What leaves the computer:

- Prompts, code and keys go only to the model providers you choose.
- Luminary Analytics receives only the update check, a download of the
  update feed. Settings > Updates can turn it off.
- Nothing else is reported to Luminary Analytics. The app's own records stay
  on the computer.

Lumi is commercial software: © Luminary Analytics, all rights reserved,
licensed under the [Lumi End User License Agreement](../lumi/legal/EULA.md)
(and, for pre-release builds, the
[Alpha and Beta Test Terms](../lumi/legal/ALPHA-TERMS.md)), which grants free
individual use during the alpha. Versions 0.6.3 through 0.19.x were published
under the MIT License and remain under it. The Extension SDK is under the
[Lumi Extension SDK License](../sdk/LICENSE), so others can build and ship
extensions; the VS Code extension is part of Lumi. The installer ships the
licenses of its third-party components in `THIRD_PARTY_NOTICES.txt`.
**Settings > About Lumi** shows the version, this summary, who accepted the
terms, and opens the terms, the [privacy notice](../lumi/legal/PRIVACY.md)
and those notices, offline.

## Teams and organizations (planned)

Lumi Cloud will add a control plane for organizations:

- policy delivered to every install;
- single sign-on;
- member management;
- an approved-models catalog;
- device enrollment;
- audit export;
- usage and cost reporting.

Until it exists, organizations set policy on each computer (see
[Organization policy](enterprise-policy.md)) and deploy with the MSI (see
[Deploying on Windows](deploy-windows.md)).

How Lumi Cloud is priced, and whether a paid tier adds anything to the app
itself, is still to be decided.
