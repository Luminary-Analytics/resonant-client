"""Lumi's legal texts (packaging/legal_texts.py): one file of facts rendered into every text Lumi ships, the
check that keeps them current, the release check for facts still to be provided, the installers' RTF, and
how the texts ship."""

from __future__ import annotations

import copy
import importlib.util
import json
import re
from pathlib import Path

import pytest

from lumi import terms

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("legal_texts", ROOT / "packaging" / "legal_texts.py")
legal_texts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(legal_texts)

COMPLETE = {
    "entity": {"legal_name": "Luminary Analytics LLC", "type": "limited liability company",
               "state_of_formation": "Delaware"},
    "governing_law": "Delaware", "venue": "New Castle County, Delaware", "notices_email": "legal@example.com",
}


def _complete_facts():
    facts = copy.deepcopy(legal_texts.load_facts())
    facts.update(copy.deepcopy(COMPLETE))
    return facts


def test_the_committed_texts_are_what_render_writes():
    # Edit lumi/legal/templates/ or lumi/legal/terms.json, then `python packaging/legal_texts.py render`.
    assert legal_texts.stale() == []
    assert legal_texts.main(["check"]) == 0


def test_every_fact_comes_from_the_one_file():
    facts = _complete_facts()
    texts = legal_texts.rendered(facts)
    for output, text in texts.items():
        assert "{{" not in text and "TO BE PROVIDED" not in text, output
    eula = texts["lumi/legal/EULA.md"]
    assert "Luminary Analytics LLC, a Delaware limited liability company" in eula
    assert "the laws of the State of\nDelaware" in eula or "the laws of the State of Delaware" in eula
    assert "New Castle County, Delaware" in eula and "legal@example.com" in eula
    version = facts["documents"]["eula"]["version"]
    assert f"Version {version}, effective " in eula
    assert "Copyright (c) 2026 Luminary Analytics LLC" in texts["sdk/LICENSE"]
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


@pytest.mark.parametrize("version", ["0.20.0", "0.21.0-beta.1", "0.21.0-alpha.3", "0.21.0-rc.2", "0.20.0a1",
                                     "0.20.0b2", "0.20.0rc1", "0.19.2.dev11", "0.20.0.post1", "0.20.0+abc",
                                     "1.0.0", "2.3.4-preview.1"])
def test_the_installers_and_the_app_agree_on_what_is_a_pre_release(version):
    assert legal_texts.is_prerelease(version) is terms.is_prerelease(version)


def test_facts_still_to_be_provided_warn_and_fail_a_release(monkeypatch, capsys):
    facts = legal_texts.load_facts()
    placeholders = legal_texts.placeholders(facts)
    assert legal_texts.placeholders(_complete_facts()) == []
    missing = [key for key, value in (("entity.legal_name", facts["entity"]["legal_name"]),
                                      ("notices_email", facts["notices_email"])) if "TO BE PROVIDED" in value]
    for key in missing:
        assert any(item.startswith(f"{key}: [[TO BE PROVIDED") for item in placeholders)
    # A pull request warns (in CI as annotations) and passes; a release build fails.
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert legal_texts.main(["release-check", "--version", "0.20.0"]) == 0
    out = capsys.readouterr().out
    if placeholders:
        assert out.startswith("::warning title=Legal texts::Still to be provided")
    code = legal_texts.main(["release-check", "--release", "--version", "0.20.0"])
    err = capsys.readouterr().err
    assert code == (1 if placeholders else 0)
    if placeholders:
        assert "::error title=Legal texts::Still to be provided" in err


def test_a_release_under_these_terms_is_0_20_or_later():
    assert "MIT License" in legal_texts.version_problem("0.19.3")
    assert "MIT License" in legal_texts.version_problem("0.6.3")
    for version in ("0.20.0", "0.20.0-beta.1", "0.21.4", "1.0.0"):
        assert legal_texts.version_problem(version) == ""
    monkeypatch_facts = _complete_facts()
    assert legal_texts.placeholders(monkeypatch_facts) == []


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
    # The agreement names what stays MIT.
    assert "0.6.3 through\n0.19.x" in eula or "0.6.3 through 0.19.x" in eula


def test_the_installers_license_page(tmp_path):
    stable = legal_texts.write_rtf(tmp_path / "stable", "0.20.0", _complete_facts())
    assert sorted(stable) == ["eula.rtf", "license.rtf", "privacy.rtf"]
    beta = legal_texts.write_rtf(tmp_path / "beta", "0.21.0-beta.1", _complete_facts())
    assert sorted(beta) == ["alpha-terms.rtf", "eula.rtf", "license.rtf", "privacy.rtf"]
    license_stable = stable["license.rtf"].read_bytes().decode("ascii")
    license_beta = beta["license.rtf"].read_bytes().decode("ascii")
    assert license_stable.startswith("{\\rtf1\\ansi") and license_stable.rstrip().endswith("}")
    assert "Lumi End User License Agreement" in license_stable and "Alpha and Beta Test Terms\\b0" not in license_stable
    assert "\\page" in license_beta and "This pre-release build of Lumi (0.21.0-beta.1)" in license_beta
    assert "Luminary Analytics LLC" in license_beta
    for text in (license_stable, license_beta):
        depth = 0
        for match in re.finditer(r"(?<!\\)[{}]", text):
            depth += 1 if match.group(0) == "{" else -1
            assert depth >= 0
        assert depth == 0
        assert "**" not in text and "{{" not in text
    # A stable build written where a beta's was doesn't leave the test terms behind.
    legal_texts.write_rtf(tmp_path / "beta", "0.20.0", _complete_facts())
    assert not (tmp_path / "beta" / "alpha-terms.rtf").exists()


def test_markdown_becomes_rtf():
    rtf = legal_texts.markdown_to_rtf(
        "# Title\n\nSome **bold**, *italic*, `code` and a [site](https://example.com) or [text](EULA.md).\n\n"
        "- one\n  continued\n- two {braces} \\ back\n\n1. first\n\nQuotes “like this” — and ©.\n")
    assert "\\b\\fs30 Title\\b0" in rtf and "{\\b bold}" in rtf and "{\\i italic}" in rtf and "{\\f1 code}" in rtf
    assert "site (https://example.com)" in rtf and "text or" not in rtf and "text." in rtf
    assert "\\bullet\\tab one continued\\par" in rtf and "\\{braces\\} \\\\ back" in rtf and "1.\\tab first" in rtf
    assert "\\u8220?like this\\u8221?" in rtf and "\\u8212?" in rtf and "\\u169?" in rtf
    assert rtf.isascii()


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


def test_the_sdk_and_the_editor_extension_are_no_longer_mit():
    sdk = (ROOT / "sdk" / "LICENSE").read_text(encoding="utf-8")
    assert sdk.startswith("Lumi Extension SDK License\n") and "Permission is hereby granted" not in sdk
    assert "1.0.0 of the lumi-extension Python package, remain under the MIT License" in " ".join(sdk.split())
    pyproject = (ROOT / "sdk" / "python" / "pyproject.toml").read_text(encoding="utf-8")
    assert 'version = "1.1.0"' in pyproject and '"MIT"' not in pyproject
    manifest = json.loads((ROOT / "lumi" / "code_editors" / "vscode" / "package.json").read_text(encoding="utf-8"))
    assert (manifest["license"], manifest["version"]) == ("SEE LICENSE IN LICENSE.txt", "0.2.0")
    extension = (ROOT / "lumi" / "code_editors" / "vscode" / "LICENSE.txt").read_text(encoding="utf-8")
    assert extension.startswith("Lumi for Visual Studio Code\n") and "Lumi End User License Agreement" in extension
    # Third-party notices stay as they were: ported code keeps its MIT text.
    pi = (ROOT / "packaging" / "licenses" / "pi-coding-agent-LICENSE.txt").read_text(encoding="utf-8")
    assert pi.startswith("MIT License")
