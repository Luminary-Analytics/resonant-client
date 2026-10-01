"""Behavioral checks for pure swarming policy; no worker or filesystem effects."""

from dataclasses import FrozenInstanceError, replace
import itertools
import json

import pytest

from lumi.engine.swarming.policy import (
    AssignmentGrant,
    ModelSelection,
    NATIVE_PROVIDERS,
    PolicyDenied,
    PolicyProfile,
    SwarmPolicy,
    normalize_scope,
    normalize_scopes,
    scopes_overlap,
)


def profile(**changes):
    values = {"version": 1, "allowed_tools": frozenset({"read_file", "write_file", "search"}),
              "allowed_providers": frozenset({"ollama", "sonn"}), "max_workers": 4,
              "read_roots": (".",), "write_roots": ("src", "tests")}
    return PolicyProfile(**(values | changes))


def admit(effective, **changes):
    values = {"model": ModelSelection("ollama", "explicit:model"), "tools": ("read_file",),
              "read_roots": ("src",), "write_roots": ()}
    return SwarmPolicy.admit(effective, **(values | changes))


def test_intersection_narrows_every_dimension_regardless_of_newer_parent_version():
    organization = profile(version=12, allowed_tools=frozenset({"read_file", "search"}),
                           max_workers=3, write_roots=("src/components",))
    project = profile(version=2, allowed_providers=frozenset({"sonn"}),
                      read_roots=("src", "docs"), write_roots=("src",), max_workers=2)
    user = profile(version=9, read_roots=("src/components", "tests"),
                   write_roots=("src/components/buttons",), max_workers=4)
    effective = SwarmPolicy.intersect(organization, project, user)
    assert effective == PolicyProfile(
        version=12, allowed_tools=frozenset({"read_file", "search"}),
        allowed_providers=frozenset({"sonn"}), max_workers=2,
        read_roots=("src/components",), write_roots=("src/components/buttons",),
    )
    with pytest.raises(PolicyDenied):
        admit(effective, model=ModelSelection("ollama", "explicit:model"))
    grant = admit(effective, model=ModelSelection("sonn", "sonn-auto"),
                  read_roots=("src/components/buttons",))
    assert grant.model.model == "sonn-auto"
    assert grant.policy_digest == effective.digest


def test_intersection_is_order_independent_associative_and_idempotent():
    parents = [profile(), profile(version=3, read_roots=("src/lib", "docs")),
               profile(version=2, write_roots=("tests/unit",), max_workers=1)]
    expected = SwarmPolicy.intersect(*parents)
    for order in itertools.permutations(parents):
        assert SwarmPolicy.intersect(*order) == expected
    assert SwarmPolicy.intersect(SwarmPolicy.intersect(*parents[:2]), parents[2]) == expected
    assert SwarmPolicy.intersect(expected, expected) == expected
    assert SwarmPolicy.intersect(expected) == expected


def test_disjoint_scopes_and_empty_parent_permissions_stay_denied():
    effective = SwarmPolicy.intersect(profile(read_roots=("src",), write_roots=("src/a",)),
                                      profile(read_roots=("src2",), write_roots=("src/b",)))
    assert effective.read_roots == ()
    assert effective.write_roots == ()
    with pytest.raises(PolicyDenied):
        admit(effective)
    grant = admit(effective, read_roots=(), write_roots=(), tools=())
    assert grant.read_roots == grant.write_roots == ()
    denied = SwarmPolicy.intersect(profile(), profile(allowed_tools=frozenset(),
                                                      allowed_providers=frozenset()))
    assert denied.allowed_tools == denied.allowed_providers == frozenset()
    with pytest.raises(PolicyDenied):
        admit(denied, tools=(), read_roots=())


def test_no_parent_policy_never_creates_permissive_defaults():
    with pytest.raises(ValueError):
        SwarmPolicy.intersect()


@pytest.mark.parametrize("changes", [
    {"tools": ("shell",)},
    {"read_roots": (".",)},
    {"read_roots": ("src-secret",)},
    {"read_roots": ("src2",)},
    {"read_roots": ("src", "private")},
    {"write_roots": ("src",)},
    {"model": ModelSelection("kimi", "explicit-choice")},
])
def test_requested_field_cannot_broaden_effective_policy(changes):
    effective = profile(read_roots=("src",), write_roots=("src/components",))
    with pytest.raises(PolicyDenied):
        admit(effective, **changes)


def test_subpaths_and_file_roots_are_segment_scoped():
    effective = profile(read_roots=("src/main.py",), write_roots=())
    grant = admit(effective, read_roots=("src/main.py",))
    assert grant.read_roots == ("src/main.py",)
    with pytest.raises(PolicyDenied):
        admit(effective, read_roots=("src/main.py.backup",))
    with pytest.raises(PolicyDenied):
        admit(effective, read_roots=("src/main",))


def test_read_and_write_permissions_are_independent():
    effective = profile(read_roots=(), write_roots=("output",))
    grant = admit(effective, tools=("write_file",), read_roots=(), write_roots=("output/report",))
    assert grant.read_roots == ()
    assert grant.write_roots == ("output/report",)
    with pytest.raises(PolicyDenied):
        admit(effective, read_roots=("output",))


@pytest.mark.parametrize("provider", sorted(NATIVE_PROVIDERS))
def test_native_provider_and_exact_model_are_preserved(provider):
    selected = ModelSelection(provider, "chosen/model:version")
    effective = profile(allowed_providers=frozenset({provider}))
    grant = admit(effective, model=selected)
    assert grant.model is selected
    assert grant.model.model == "chosen/model:version"


@pytest.mark.parametrize("provider", ["codex", "claude", "claude-code", "unknown", "Ollama"])
def test_unsupported_or_cli_provider_is_unavailable(provider):
    with pytest.raises(PolicyDenied, match="CLI workers are unavailable"):
        ModelSelection(provider, "explicit-choice")
    with pytest.raises(ValueError, match="native provider"):
        profile(allowed_providers=frozenset({provider}))


@pytest.mark.parametrize("roots,expected", [
    (("./src//lib/", r"src\lib", "src/lib/nested"), ("src/lib",)),
    (("src", "tests", "src/lib", "src"), ("src", "tests")),
    (("./", "src"), (".",)),
    (("src/lib", "src/lib2"), ("src/lib", "src/lib2")),
    ((), ()),
])
def test_path_normalization_preserves_scope_meaning(roots, expected):
    assert normalize_scopes(roots) == expected


@pytest.mark.parametrize("path", [
    "", "/absolute", r"\absolute", r"\\host\share", "//host/share", "C:/root", r"C:\root",
    "C:relative", "src/../secret", r"src\..\secret", "..", "./../secret", "src/*", "src/?",
    "src/[ab]", "src/{one,two}", "src/file:stream", "src/file\x00.txt", "src\nfile", "src\tfile",
    "src.", "src/part ", "src/CON", "src/nul.txt", "src/COM1", "src/lpt3.log", 'src/"quoted"',
    "src/<part>", "src/a|b",
])
def test_unsafe_or_ambiguous_scope_spellings_are_rejected(path):
    with pytest.raises(ValueError):
        normalize_scope(path)


def test_overlap_helper_handles_segments_empty_scopes_and_windows_case():
    assert scopes_overlap((".",), ("anything",))
    assert scopes_overlap(("src",), ("src/lib",))
    assert scopes_overlap(("src/lib",), ("src",))
    assert not scopes_overlap(("src",), ("src2",))
    assert not scopes_overlap((), (".",))
    assert not scopes_overlap(("Src",), ("src/lib",))
    assert scopes_overlap(("Src",), ("src/lib",), case_sensitive=False)
    with pytest.raises(PolicyDenied):
        admit(profile(read_roots=("Src",)), read_roots=("src",))


@pytest.mark.parametrize("value", [0, 9, -1, True, 2.0, "2"])
def test_worker_limit_is_a_visible_one_to_eight_integer(value):
    with pytest.raises(ValueError, match="worker slots"):
        profile(max_workers=value)


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "1"])
def test_version_is_positive_integer(value):
    with pytest.raises(ValueError, match="versions"):
        profile(version=value)


def test_policy_and_grant_round_trip_with_canonical_digests():
    effective = profile(read_roots=(r"src\lib", "src", "tests"))
    reordered = profile(read_roots=("tests", "src"),
                        allowed_tools=frozenset({"search", "write_file", "read_file"}))
    assert effective.digest == reordered.digest
    assert len(effective.digest) == 64
    assert PolicyProfile.from_dict(json.loads(json.dumps(effective.to_dict()))) == effective
    grant = admit(effective, tools=("search", "read_file"), read_roots=(r"src\lib",))
    assert AssignmentGrant.from_dict(json.loads(json.dumps(grant.to_dict()))) == grant
    assert ModelSelection.from_dict(grant.model.to_dict()) == grant.model
    assert grant.to_dict()["tools"] == ["read_file", "search"]
    assert replace(effective, max_workers=1).digest != effective.digest
    assert replace(effective, version=2).digest != effective.digest
    with pytest.raises(FrozenInstanceError):
        effective.max_workers = 1
    with pytest.raises(FrozenInstanceError):
        grant.policy_version = 99


@pytest.mark.parametrize("kind", ["profile", "grant", "model", "nested_model"])
def test_credential_like_extras_are_rejected_without_echo(kind):
    effective = profile()
    grant = admit(effective)
    factory, payload = {
        "profile": (PolicyProfile.from_dict, effective.to_dict()),
        "grant": (AssignmentGrant.from_dict, grant.to_dict()),
        "model": (ModelSelection.from_dict, grant.model.to_dict()),
        "nested_model": (AssignmentGrant.from_dict, grant.to_dict()),
    }[kind]
    target = payload["model"] if kind == "nested_model" else payload
    target["api_key"] = "secret-fixture-value"
    with pytest.raises(ValueError) as failure:
        factory(payload)
    assert "secret-fixture-value" not in str(failure.value)
    assert "api_key" not in str(failure.value)
    assert "secret-fixture-value" not in json.dumps(grant.to_dict())


@pytest.mark.parametrize("changes", [
    {"allowed_tools": "read_file"}, {"allowed_tools": [1]}, {"allowed_tools": ["search", "search"]},
    {"allowed_providers": {"ollama": True}}, {"read_roots": "src"}, {"write_roots": [1]},
    {"max_workers": True}, {"version": "1"},
])
def test_deserialization_rejects_wrong_types_instead_of_coercing(changes):
    serialized = profile().to_dict() | changes
    with pytest.raises(ValueError):
        PolicyProfile.from_dict(serialized)


def test_missing_restriction_does_not_restore_a_default():
    serialized = profile().to_dict()
    del serialized["write_roots"]
    with pytest.raises(ValueError):
        PolicyProfile.from_dict(serialized)


def test_grant_requires_model_identity_digest_and_strict_permissions():
    serialized = admit(profile()).to_dict()
    for changes in ({"policy_digest": "forged"}, {"policy_version": True},
                    {"model": {"provider": "ollama"}}, {"tools": "read_file"},
                    {"read_roots": ["../outside"]}, {"write_roots": None}):
        with pytest.raises(ValueError):
            AssignmentGrant.from_dict(serialized | changes)


def test_model_selection_rejects_missing_or_implicit_values():
    for changes in ({"model": ""}, {"model": None}, {"provider": ""}, {"model": " name "}):
        with pytest.raises(ValueError):
            ModelSelection.from_dict({"provider": "ollama", "model": "chosen"} | changes)
