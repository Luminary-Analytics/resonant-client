"""Lumi's legal texts (packaging/legal_texts.py): one file of facts rendered into every text Lumi ships, the
check that keeps them current and pinned to their versions, the release check, the installers' RTF and the
versions it holds, what the texts say about the earlier MIT copies and what leaves the computer, and how the
texts ship."""

from __future__ import annotations

import copy
import importlib.util
import json
import re
import shutil
from datetime import date, timedelta
from pathlib import Path

import pytest

from lumi import terms

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("legal_texts", ROOT / "packaging" / "legal_texts.py")
legal_texts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(legal_texts)

# The owner's facts (September 29, 2026): what every text must name.
OWNER = {
    "entity": {"legal_name": "Luminary Analytics, LLC", "type": "limited liability company",
               "state_of_formation": "New Hampshire"},
    "governing_law": "New Hampshire", "venue": "New Hampshire",
    "notices_email": "rich.bellantoni@luminaryanalytics.com",
}


def _flat(text: str) -> str:
    """A text with its line wrapping undone, for phrases that cross a line."""
    return " ".join(text.split())


def _facts(**changes):
    facts = copy.deepcopy(legal_texts.load_facts())
    facts.update(copy.deepcopy(changes))
    return facts


def test_the_committed_texts_are_what_render_writes():
    # Edit lumi/legal/templates/ or lumi/legal/terms.json, then `python packaging/legal_texts.py render`.
    assert legal_texts.stale() == []
    assert legal_texts.main(["check"]) == 0


def test_the_owners_facts_are_filled_in_and_nothing_is_left_to_provide():
    facts = legal_texts.load_facts()
    assert {key: facts[key] for key in OWNER} == OWNER
    assert legal_texts.placeholders() == [] and legal_texts.unfinished() == []
    texts = legal_texts.rendered()
    for output, text in texts.items():
        assert "TO BE PROVIDED" not in text and "{{" not in text, output
    eula = _flat(texts["lumi/legal/EULA.md"])
    assert "Luminary Analytics, LLC, a New Hampshire limited liability company" in eula
    assert ("is governed by the laws of the State of New Hampshire and applicable United States federal law"
            in eula)
    assert "The state and federal courts located in New Hampshire have exclusive jurisdiction" in eula
    # The consumer carve-out stays.
    assert "If you are a consumer and the law of the place where you live gives you rights" in eula
    assert "rich.bellantoni@luminaryanalytics.com" in eula
    sdk = _flat(texts["sdk/LICENSE"])
    assert "Copyright (c) 2026 Luminary Analytics, LLC" in sdk
    assert "the laws of the State of New Hampshire and applicable United States federal law" in sdk
    assert "state and federal courts located in New Hampshire" in sdk
    # The release check passes with these values (the release workflow runs it for a release version).
    assert legal_texts.release_problems("0.20.0", release=True) == []
    assert legal_texts.main(["release-check", "--release", "--version", "0.20.0"]) == 0


def test_every_fact_comes_from_the_one_file():
    facts = _facts(entity={"legal_name": "Example Holdings, LLC", "type": "limited liability company",
                           "state_of_formation": "Vermont"},
                   governing_law="Vermont", venue="Chittenden County, Vermont", notices_email="legal@example.com")
    texts = legal_texts.rendered(facts)
    for output, text in texts.items():
        assert "{{" not in text and "TO BE PROVIDED" not in text, output
    eula = _flat(texts["lumi/legal/EULA.md"])
    assert "Example Holdings, LLC, a Vermont limited liability company" in eula
    assert "the laws of the State of Vermont" in eula
    assert "Chittenden County, Vermont" in eula and "legal@example.com" in eula
    version = facts["documents"]["eula"]["version"]
    assert f"Version {version}, published " in eula
    assert "Copyright (c) 2026 Example Holdings, LLC" in texts["sdk/LICENSE"]
    assert texts["sdk/LICENSE"] == texts["sdk/python/lumi_extension/LICENSE"]
    with pytest.raises(legal_texts.LegalTextError, match="isn't in lumi/legal/terms.json"):
        legal_texts.render_text("{{entity.nickname}}", facts)
    with pytest.raises(legal_texts.LegalTextError, match="names a group"):
        legal_texts.render_text("{{entity}}", facts)


def test_the_app_reads_the_same_facts_and_texts():
    for doc_id in terms.READABLE_DOCUMENTS:
        entry = legal_texts.load_facts()["documents"][doc_id]
        assert terms.document(doc_id).version == entry["version"]
        assert terms.text(doc_id) == legal_texts.rendered()[f"lumi/legal/{entry['file']}"].split("\n", 1)[1]
        # The app hashes a text as the pin does.
        assert terms.text_sha256(doc_id) == legal_texts.text_sha256(doc_id) == entry["sha256"]


def test_each_version_pins_its_text(monkeypatch, tmp_path, capsys):
    """A change to what a text says without a new version fails: the pin in terms.json is that version's text."""
    assert legal_texts.pin_problems() == []
    templates = tmp_path / "templates"
    shutil.copytree(legal_texts.TEMPLATES, templates)
    eula = templates / "EULA.md"
    eula.write_text(eula.read_text(encoding="utf-8").replace("Lumi is free for individuals",
                                                             "Lumi is free for everyone"), encoding="utf-8")
    monkeypatch.setattr(legal_texts, "TEMPLATES", templates)
    [problem] = legal_texts.pin_problems()
    assert "lumi/legal/EULA.md isn't the text pinned for eula version '1.0'" in problem
    assert "raise documents.eula.version" in problem
    assert legal_texts.text_sha256("eula") in problem
    # The release check fails on it too, and `check` exits 1 once the text is rendered.
    assert any("isn't the text pinned" in item for item in legal_texts.release_problems("0.20.0", release=True))
    monkeypatch.setattr(legal_texts, "stale", lambda texts=None: [])
    assert legal_texts.main(["check"]) == 1
    assert "isn't the text pinned for eula" in capsys.readouterr().err
    # A new version with its pin passes (its number is part of the text it pins).
    facts = copy.deepcopy(legal_texts.load_facts())
    facts["documents"]["eula"]["version"] = "1.1"
    facts["documents"]["eula"]["sha256"] = legal_texts.text_sha256("eula", legal_texts.rendered(facts))
    assert legal_texts.pin_problems(facts) == []


def test_a_placeholder_written_into_a_template_fails_the_release(monkeypatch, tmp_path):
    """The PR #104 review's probe: placeholders() read only terms.json, so one in a template shipped."""
    templates = tmp_path / "templates"
    shutil.copytree(legal_texts.TEMPLATES, templates)
    eula = templates / "EULA.md"
    eula.write_text(eula.read_text(encoding="utf-8") + "\n[[TO BE PROVIDED: signature block]]\n", encoding="utf-8")
    monkeypatch.setattr(legal_texts, "TEMPLATES", templates)
    monkeypatch.setattr(legal_texts, "stale", lambda texts=None: [])  # as if rendered and committed
    assert "[[TO BE PROVIDED: signature block]]" in legal_texts.rendered()["lumi/legal/EULA.md"]
    problems = legal_texts.release_problems("0.20.0", release=True)
    assert "Still to be provided in lumi/legal/EULA.md: [[TO BE PROVIDED: signature block]]" in problems


def test_facts_still_to_be_provided_warn_and_fail_a_release(monkeypatch, capsys):
    facts = _facts(notices_email="[[TO BE PROVIDED: notices email address]]")
    assert legal_texts.placeholders(facts) == ["notices_email: [[TO BE PROVIDED: notices email address]]"]
    monkeypatch.setattr(legal_texts, "load_facts", lambda path=None: facts)
    # A pull request warns (in CI as annotations) and passes; a release build fails.
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert legal_texts.main(["release-check", "--version", "0.20.0"]) == 0
    assert capsys.readouterr().out.startswith("::warning title=Legal texts::Still to be provided")
    assert legal_texts.main(["release-check", "--release", "--version", "0.20.0"]) == 1
    assert "::error title=Legal texts::Still to be provided" in capsys.readouterr().err


def test_no_text_is_published_after_the_day_of_the_build():
    """A version applies from the day it's accepted, so no text may carry a date after someone could accept it."""
    assert legal_texts.date_problems() == []
    published = date.fromisoformat(legal_texts.load_facts()["documents"]["eula"]["published"])
    [problem, *_rest] = legal_texts.date_problems(today=published - timedelta(days=1))
    assert "documents.eula.published" in problem and "after this build's day" in problem
    for doc_id in terms.ACCEPTED_DOCUMENTS:
        text = _flat(terms.text(doc_id))
        assert "applies to you from the day you accept it" in text
        assert not re.search(r"\beffective (?:on )?[A-Z][a-z]+ \d", text), doc_id


@pytest.mark.parametrize("version", ["0.20.0", "0.21.0-beta.1", "0.21.0-alpha.3", "0.21.0-rc.2", "0.20.0a1",
                                     "0.20.0b2", "0.20.0rc1", "0.19.2.dev11", "0.20.0.dev0", "0.20.0.post1",
                                     "0.20.0+abc", "1.0.0", "2.3.4-preview.1"])
def test_the_installers_and_the_app_agree_on_what_is_a_pre_release(version):
    assert legal_texts.is_prerelease(version) is terms.is_prerelease(version)


def test_a_release_is_x_y_z_or_a_hyphenated_pre_release_from_0_20():
    for version in ("0.20.0", "0.20.0-alpha.1", "0.20.0-beta.1", "0.21.4-rc.2", "1.0.0"):
        assert legal_texts.version_problem(version) == "", version
    # PEP 440 spellings carry no hyphen: GitHub, the feeds and the installers would take them for stable.
    for version in ("0.20.0rc1", "0.20.0b1", "0.20.0a1", "0.20.0.dev0", "0.20.0-beta", "v0.20.0", "0.20"):
        assert "isn't a release version" in legal_texts.version_problem(version), version
    for version in ("0.19.3", "0.6.3", "0.19.2-beta.1"):
        problem = legal_texts.version_problem(version)
        assert "start at 0.20.0" in problem and "MIT License" in problem, version


def test_the_release_workflow_tells_a_pre_release_the_same_way():
    """release.yml checks the tag with the release check's pattern and flags pre-releases by it, so a tag such
    as v0.20.0rc1 can't publish a non-prerelease GitHub Release (the PR #104 review)."""
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    patterns = re.findall(r'"\$VERSION" =~ \^(.+?)\$ \]\]', workflow)
    assert len(patterns) == 2 and len(set(patterns)) == 1  # the Windows and macOS jobs
    shell = re.compile(patterns[0].replace("[0-9]", r"\d"))
    for version in ("0.20.0", "0.20.0-beta.1", "0.20.0-rc.3", "0.20.0rc1", "0.20.0.dev0", "0.20.0-preview.1"):
        assert bool(shell.fullmatch(version)) is bool(legal_texts.RELEASE_VERSION.fullmatch(version)), version
    assert "contains(steps.version.outputs.version, '-')" not in workflow
    assert "prerelease: ${{ steps.version.outputs.prerelease == 'true' }}" in workflow
    assert workflow.count("if: ${{ steps.version.outputs.prerelease == 'false' }}") == 2  # the MSI and its signing
    assert workflow.count('if [[ "$VERSION" == *-* ]]; then PRERELEASE=true; else PRERELEASE=false; fi') == 2


def test_the_texts_carry_no_drafting_notes():
    # They ship and take effect as they are: no draft banners or notes for reviewers.
    texts = legal_texts.rendered()
    for output, text in texts.items():
        for marker in ("DRAFT", "Draft", "not in effect", "TODO", "TBD", "counsel", "review note"):
            assert marker not in text, (output, marker)


def test_what_the_texts_promise_matches_how_lumi_asks():
    eula = terms.text("eula")
    alpha = terms.text("alpha_terms")
    for text in (eula, alpha):
        # The ways the app and the command line take an acceptance.
        assert "`lumi terms accept`" in text and "`--accept-terms`" in text and "`LUMI_ACCEPT_TERMS`" in text
    assert terms.ENVIRONMENT == "LUMI_ACCEPT_TERMS"
    # The alpha terms say how to leave: uninstall and delete ~/.lumi.
    assert "`~/.lumi`" in alpha and "uninstall Lumi" in alpha
    # A pre-release build is one whose version carries the label, whatever channel brought it.
    for text in (_flat(eula), _flat(alpha)):
        assert "The label alone decides" in text and "even when it reaches you on the beta update channel" in text
    # An organization accepts only through the machine policy (EULA 2.4 and 16.2, the alpha terms).
    flat = _flat(eula)
    assert "by setting Lumi's machine policy for that purpose on each computer" in flat
    assert "No other policy can accept for anyone" in flat and "delivered through Lumi Cloud" in flat
    assert "accepted Lumi's terms through its machine policy, as section 2.4 describes" in flat
    assert "only through Lumi's machine policy, as section 2.4 of the EULA describes" in _flat(alpha)
    # Which new versions ask again (EULA 16.2), and every change is a new version (16.3), as the pins enforce.
    assert "A new version of the Privacy Notice or of the Lumi Extension SDK License doesn't ask you" in flat
    assert "Any change, including a correction, comes as a new version" in flat
    # Computer use is on by default (EULA 6.1).
    assert "Computer use is on unless you turn it off" in flat
    # The alpha terms leave feedback's details to the privacy notice, so they can change without re-acceptance.
    assert "Include diagnostics" not in alpha and "The Privacy Notice describes what a report contains" in _flat(alpha)


def test_the_mit_copies_are_named_by_release_and_commit_not_a_version_range():
    """0.19.2.dev11 was built both under the MIT License and after it, so the carve-out names releases and
    commits (the PR #104 review), and this build's version is past every MIT version."""
    eula = _flat(terms.text("eula"))
    assert "the releases tagged v0.6.3a1 through v0.19.1, published from May 15 to September 13, 2026" in eula
    assert "Resonant Client (0.6.3a1 through 0.6.10), Resonant (0.6.11 through 0.18.2) and SONN Client" in eula
    assert "commit c00f29c of May 15, 2026" in eula and "commit beb2848 of September 27, 2026" in eula
    assert "The releases tagged v0.2.0 through v0.6.2 were not published under the MIT License" in eula
    assert "Neither was part of a release" in eula
    texts = {name: (ROOT / name).read_text(encoding="utf-8") for name in (
        "LICENSE", "README.md", "RELEASING.md", "pyproject.toml", "lumi/legal/EULA.md", "sdk/LICENSE",
        "lumi/code_editors/vscode/LICENSE.txt", "packaging/legal_texts.py", "docs/plans.md", "docs/extensions.md")}
    for name, text in texts.items():
        assert not re.search(r"0\.19\.x|up to (?:and including )?(?:version )?(?:0\.1\.0|1\.0\.0)", _flat(text)), name
    license_text = _flat(texts["LICENSE"])
    assert "v0.6.3a1 through v0.19.1" in license_text and "c00f29c" in license_text and "beb2848" in license_text
    # This build is none of them.
    assert legal_texts.lumi_version() == "0.20.0.dev0" == terms.this_version()
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'version = "0.20.0.dev0"' in pyproject


def test_the_privacy_notice_says_what_the_code_sends():
    """Each claim the PR #104 review found the code contradicting, checked against what the notice says now."""
    privacy = _flat(terms.text("privacy"))
    # A policy's DLP service receives the text of model requests.
    assert "Lumi sends that service the text of each request" in privacy
    # Lumi Cloud receives shared conversations, hand-offs, chat-task replies, approvals, usage and crash counts.
    for phrase in ("a shared conversation", "a hand-off", "sends back Lumi's reply",
                   "a second person's approval of a command: the command", "counts of turns and how they ended",
                   "of crashes of the app"):
        assert phrase in privacy, phrase
    # Unattended work runs and is recorded by default (oversight.unattended: record).
    assert "Otherwise, by default, it runs without asking and is recorded as unattended" in privacy
    # A security flag's excerpt can come from what a tool returned.
    assert "a flag carries a short excerpt of the text that raised it, which can come from what a tool returned" \
        in privacy
    # Feedback as PR #101 sends it, when the feature is available.
    for phrase in ("When the feedback feature is available", "`privacy.feedback_url`", "carries your sign-in",
                   "Lumi tries again in the background", "turn diagnostics, or feedback itself, off"):
        assert phrase in privacy, phrase
    # What the notice no longer claims.
    for claim in ("Luminary doesn't receive them", "sends nothing to a model until you confirm it. Then",
                  "never sends file contents, what tools returned", "no analytics, usage telemetry, advertising"):
        assert claim not in privacy, claim


def test_the_installers_license_page(tmp_path):
    stable = legal_texts.write_rtf(tmp_path / "stable", "0.20.0")
    assert sorted(stable) == ["eula.rtf", "license-versions.iss", "license-versions.json", "license.rtf",
                              "privacy.rtf"]
    beta = legal_texts.write_rtf(tmp_path / "beta", "0.21.0-beta.1")
    assert sorted(beta) == ["alpha-terms.rtf", "eula.rtf", "license-versions.iss", "license-versions.json",
                            "license.rtf", "privacy.rtf"]
    license_stable = stable["license.rtf"].read_bytes().decode("ascii")
    license_beta = beta["license.rtf"].read_bytes().decode("ascii")
    assert license_stable.startswith("{\\rtf1\\ansi") and license_stable.rstrip().endswith("}")
    assert "Lumi End User License Agreement" in license_stable and "Alpha and Beta Test Terms\\b0" not in license_stable
    assert "\\page" in license_beta and "This pre-release build of Lumi (0.21.0-beta.1)" in license_beta
    assert "Luminary Analytics, LLC" in license_beta
    for text in (license_stable, license_beta):
        depth = 0
        for match in re.finditer(r"(?<!\\)[{}]", text):
            depth += 1 if match.group(0) == "{" else -1
            assert depth >= 0
        assert depth == 0
        assert "**" not in text and "{{" not in text
        # An MSI would read [name] as a property: no bracket is written as one.
        assert "[" not in text and "]" not in text
    # The versions license.rtf holds, for the installer's record (packaging/installer.iss).
    eula = legal_texts.load_facts()["documents"]["eula"]["version"]
    alpha = legal_texts.load_facts()["documents"]["alpha_terms"]["version"]
    assert json.loads(stable["license-versions.json"].read_text(encoding="ascii"))["documents"] == \
        {"eula": eula, "alpha_terms": ""}
    assert json.loads(beta["license-versions.json"].read_text(encoding="ascii"))["documents"] == \
        {"eula": eula, "alpha_terms": alpha}
    iss = beta["license-versions.iss"].read_text(encoding="ascii")
    assert f'#define LicenseEulaVersion "{eula}"' in iss and f'#define LicenseAlphaTermsVersion "{alpha}"' in iss
    assert '#define LicenseAlphaTermsVersion ""' in stable["license-versions.iss"].read_text(encoding="ascii")
    # A stable build written where a beta's was doesn't leave the test terms behind.
    legal_texts.write_rtf(tmp_path / "beta", "0.20.0")
    assert not (tmp_path / "beta" / "alpha-terms.rtf").exists()


def test_markdown_becomes_rtf():
    rtf = legal_texts.markdown_to_rtf(
        "# Title\n\nSome **bold**, *italic*, `code` and a [site](https://example.com) or [text](EULA.md).\n\n"
        "- one\n  continued\n- two {braces} \\ back [bracketed]\n\n1. first\n\nQuotes “like this” — and ©.\n")
    assert "\\b\\fs30 Title\\b0" in rtf and "{\\b bold}" in rtf and "{\\i italic}" in rtf and "{\\f1 code}" in rtf
    assert "site (https://example.com)" in rtf and "text or" not in rtf and "text." in rtf
    assert "\\bullet\\tab one continued\\par" in rtf and "\\{braces\\} \\\\ back" in rtf and "1.\\tab first" in rtf
    assert "\\'5bbracketed\\'5d" in rtf and "[" not in rtf
    assert "\\u8220?like this\\u8221?" in rtf and "\\u8212?" in rtf and "\\u169?" in rtf
    assert rtf.isascii()


def test_the_exe_installer_shows_the_terms_once_per_version():
    """PR #104 review item 7: every EXE update showed the license page. installer.iss records the versions it
    showed and skips the page while they match; the record is never a person's acceptance."""
    iss = (ROOT / "packaging" / "installer.iss").read_text(encoding="utf-8")
    assert '#include "..\\dist\\legal\\license-versions.iss"' in iss
    registry = [line for line in iss.splitlines() if line.startswith("Root: HKLM64;")]
    assert len(registry) == 2
    for line, name, value in zip(registry, ("LicenseEulaVersion", "LicenseAlphaTermsVersion"),
                                 ("{#LicenseEulaVersion}", "{#LicenseAlphaTermsVersion}")):
        assert 'Subkey: "SOFTWARE\\Luminary Analytics\\Lumi\\Setup"' in line
        assert f'ValueName: "{name}"; ValueData: "{value}"' in line
        # A silent install shows no page, so it records nothing.
        assert line.endswith("Check: not WizardSilent")
    assert "function ShouldSkipPage(PageID: Integer): Boolean;" in iss and "if PageID = wpLicense then" in iss
    code = iss[iss.index("function LicenseAlreadyShown(): Boolean;"):iss.index("function ShouldSkipPage")]
    # The EULA must match, and the test terms too when this build has them (a stable-to-beta update asks).
    assert "if eula <> '{#LicenseEulaVersion}' then" in code
    assert "if '{#LicenseAlphaTermsVersion}' <> '' then" in code
    assert "if alphaTerms <> '{#LicenseAlphaTermsVersion}' then" in code
    # No silent arguments to get past the page, and Lumi never reads the installer's record.
    assert "/SILENT" not in (ROOT / "packaging" / "update_appcast.py").read_text(encoding="utf-8")
    for path in (ROOT / "lumi").rglob("*.py"):
        assert "Lumi\\Setup" not in path.read_text(encoding="utf-8") and "LicenseEulaVersion" not in \
            path.read_text(encoding="utf-8"), path


def test_the_texts_ship_everywhere_lumi_does():
    spec = (ROOT / "packaging" / "lumi.spec").read_text(encoding="utf-8")
    assert '"terms.json", "EULA.md", "ALPHA-TERMS.md", "PRIVACY.md"' in spec and '"terms_view.js"' in spec
    for name in ("bundle-policy.json", "bundle-policy-macos.json", "bundle-policy-linux.json"):
        required = json.loads((ROOT / "packaging" / name).read_text(encoding="utf-8"))["required_globs"]
        for item in ("terms.json", "EULA.md", "ALPHA-TERMS.md", "PRIVACY.md"):
            assert f"_internal/lumi/legal/{item}" in required, (name, item)
        assert "_internal/lumi/gui/static/terms_view.js" in required, name
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"legal/*.md", "legal/terms.json"' in pyproject
    # The installers show the license page from the RTF that build scripts render.
    iss = (ROOT / "packaging" / "installer.iss").read_text(encoding="utf-8")
    assert "LicenseFile=..\\dist\\legal\\license.rtf" in iss
    wxs = (ROOT / "packaging" / "lumi.wxs").read_text(encoding="utf-8")
    assert "WixUILicenseRtf" in wxs and "WixUI_Minimal" in wxs
    macos = (ROOT / "packaging" / "build_macos.sh").read_text(encoding="utf-8")
    assert "legal_texts.py rtf" in macos and "--license" in macos
    # Pull request CI compiles installer.iss (its [Code] and the versions it includes) without a release.
    check = (ROOT / ".github" / "workflows" / "build-check.yml").read_text(encoding="utf-8")
    assert '& $iscc /O- "/DAppVersion=$version" "packaging/installer.iss"' in check


def test_the_sdk_and_the_editor_extension_are_no_longer_mit():
    sdk = (ROOT / "sdk" / "LICENSE").read_text(encoding="utf-8")
    assert sdk.startswith("Lumi Extension SDK License\n") and "Permission is hereby granted" not in sdk
    flat = _flat(sdk)
    assert "The SDK was never part of a Lumi release" in flat
    assert "with the lumi-extension Python package at version 1.0.0. Those copies remain under the MIT License" \
        in flat
    # People who install an Extension may run the SDK parts in it; the code the templates start is the developer's.
    assert "Anyone who receives an Extension that includes parts of the SDK" in flat
    assert "may install, run and use those parts as part of that Extension with Lumi" in flat
    assert "is yours once it's in your Extension" in flat and "section 4 doesn't apply to it" in flat
    pyproject = (ROOT / "sdk" / "python" / "pyproject.toml").read_text(encoding="utf-8")
    assert 'version = "1.1.0"' in pyproject and '"MIT"' not in pyproject
    manifest = json.loads((ROOT / "lumi" / "code_editors" / "vscode" / "package.json").read_text(encoding="utf-8"))
    assert (manifest["license"], manifest["version"]) == ("SEE LICENSE IN LICENSE.txt", "0.2.0")
    extension = (ROOT / "lumi" / "code_editors" / "vscode" / "LICENSE.txt").read_text(encoding="utf-8")
    assert extension.startswith("Lumi for Visual Studio Code\n") and "Lumi End User License Agreement" in extension
    assert "The extension was never part of a Lumi release" in _flat(extension)
    # Third-party notices stay as they were: ported code keeps its MIT text.
    pi = (ROOT / "packaging" / "licenses" / "pi-coding-agent-LICENSE.txt").read_text(encoding="utf-8")
    assert pi.startswith("MIT License")
