from __future__ import annotations

from pathlib import Path
import importlib.util


def _module():
    path = Path(__file__).parents[1] / "packaging" / "check_bundle.py"
    spec = importlib.util.spec_from_file_location("resonant_bundle_check", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_bundle_policy_manifest_and_required_assets(tmp_path):
    bundle = tmp_path / "bundle"
    (bundle / "_internal").mkdir(parents=True)
    (bundle / "resonant.exe").write_bytes(b"exe")
    (bundle / "_internal" / "required.dll").write_bytes(b"dll")
    policy = {
        "max_total_bytes": 10,
        "max_file_count": 3,
        "required_globs": ["resonant.exe", "_internal/required.dll"],
        "forbidden_path_components": ["openai"],
    }

    manifest, errors = _module().inspect_bundle(bundle, policy)

    assert errors == []
    assert manifest["file_count"] == 2
    assert manifest["total_bytes"] == 6
    assert all(len(item["sha256"]) == 64 for item in manifest["files"])


def test_bundle_policy_rejects_bloat_missing_and_forbidden_dependency(tmp_path):
    bundle = tmp_path / "bundle"
    (bundle / "_internal" / "openai").mkdir(parents=True)
    (bundle / "_internal" / "openai" / "sdk.py").write_bytes(b"12345")
    policy = {
        "max_total_bytes": 4,
        "max_file_count": 0,
        "required_globs": ["resonant.exe"],
        "forbidden_path_components": ["openai"],
    }

    _, errors = _module().inspect_bundle(bundle, policy)

    assert any("exceeds policy" in error for error in errors)
    assert any("missing" in error for error in errors)
    assert any("Forbidden dependency" in error for error in errors)
