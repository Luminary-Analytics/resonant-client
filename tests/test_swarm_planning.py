"""Untrusted coordinator proposals stay inert, scoped and deeply immutable."""

from dataclasses import FrozenInstanceError, replace
import json

import pytest

from lumi.engine.execution_guard import FILE_TOOL_NAMES, SWARM_TOOL_NAMES, WRITE_TOOL_NAMES
from lumi.engine.swarming.planning import CoordinatorPlan, PlanRejected, parse_plan
from lumi.engine.swarming.policy import ModelSelection, PolicyDenied, PolicyProfile


MODEL = ModelSelection("ollama", "explicit-fixture")
POLICY = PolicyProfile(1, FILE_TOOL_NAMES | SWARM_TOOL_NAMES | WRITE_TOOL_NAMES,
                       frozenset({"ollama"}), read_roots=(".",), write_roots=("backend", "frontend", "tests"))
CRITERIA = frozenset({"owner_review", "csv_acceptance", "frontend_acceptance", "regression_suite"})


def work(logical_id="investigate", **changes):
    return {"id": logical_id, "objective": "Inspect the CSV boundary and report retained evidence.",
            "role": "explore", "dependencies": [], "read_roots": ["backend"],
            "write_roots": [], "criteria": ["owner_review"], **changes}


def proposal(items=None, **changes):
    return {"summary": "Independent questions can be investigated separately.", "use_team": True,
            "work_items": items if items is not None else [work()], **changes}


def parse(value, **changes):
    return parse_plan(value if isinstance(value, str) else json.dumps(value), run_id="run-fixture",
                      policy=changes.pop("policy", POLICY), model=changes.pop("model", MODEL),
                      allowed_criteria=changes.pop("allowed_criteria", CRITERIA), **changes)


def test_realistic_readers_have_scoped_stable_contracts_and_derived_tools():
    value = proposal([work("backend"), work("frontend", read_roots=["frontend"])])
    result = parse(value)
    assert isinstance(result, CoordinatorPlan)
    assert result.summary == value["summary"] and result.use_team
    assert [item.logical_id for item in result.work_items] == ["backend", "frontend"]
    assert all(set(item.tools) == FILE_TOOL_NAMES | SWARM_TOOL_NAMES for item in result.work_items)
    assert all(item.write_roots == () and item.criteria == ("owner_review",) for item in result.work_items)
    assert result.work_items[0].read_roots == ("backend",)
    assert all(set(item) == {"id", "objective", "role", "dependencies", "read_roots", "write_roots", "tools", "criteria"}
               for item in result.to_work_items())


def test_two_writers_and_dependent_verifier_preserve_independent_trusted_criteria():
    items = [work("backend", role="implement", write_roots=["backend"], criteria=["csv_acceptance"]),
             work("frontend", role="implement", read_roots=["frontend"], write_roots=["frontend"], criteria=["frontend_acceptance"]),
             work("verify", role="verify", dependencies=["frontend", "backend"], read_roots=["backend", "frontend", "tests"],
                  criteria=["regression_suite"])]
    result = parse(proposal(items))
    backend, frontend, verifier = result.work_items
    assert verifier.dependencies == tuple(sorted((backend.id, frontend.id)))
    assert set(backend.tools) == FILE_TOOL_NAMES | WRITE_TOOL_NAMES | (SWARM_TOOL_NAMES - {"swarm_submit"})
    assert "swarm_submit" not in frontend.tools
    assert not set(verifier.tools) & WRITE_TOOL_NAMES
    assert verifier.write_roots == ()


def test_explicit_followup_namespaces_preserve_legacy_ids_and_rebind_only_new_dependencies():
    value = proposal([work("inspect"), work("verify", dependencies=["inspect"])])
    legacy = parse(value)
    first = parse(value, namespace="attempt-first")
    second = parse(value, namespace="attempt-second")
    assert parse(value) == legacy and parse(value, namespace="attempt-first") == first
    assert not ({item.id for item in legacy.work_items} & {item.id for item in first.work_items})
    assert not ({item.id for item in first.work_items} & {item.id for item in second.work_items})
    assert first.work_items[1].dependencies == (first.work_items[0].id,)
    assert second.work_items[1].dependencies == (second.work_items[0].id,)


def test_serial_recommendation_is_one_contract_without_claiming_speedup():
    result = parse(proposal(use_team=False, summary="This change shares one invariant; keep one implementation owner."))
    assert result.use_team is False and len(result.work_items) == 1
    assert result.to_dict()["use_team"] is False
    with pytest.raises(PlanRejected):
        parse(proposal([work("a"), work("b")], use_team=False))


def test_ids_are_run_scoped_order_independent_and_not_derived_from_descriptions():
    first = proposal([work("a"), work("b", dependencies=["a"])])
    second = proposal(list(reversed(first["work_items"])), summary="Rephrased summary")
    original, reordered = parse(first), parse(second)
    assert original.work_items == reordered.work_items
    modified = proposal([work("a", objective="More precise task"), work("b", dependencies=["a"])])
    assert parse(modified).work_items[0].id == original.work_items[0].id
    foreign = parse_plan(json.dumps(first), run_id="other-run", policy=POLICY, model=MODEL, allowed_criteria=CRITERIA)
    assert {item.id for item in original.work_items}.isdisjoint(item.id for item in foreign.work_items)
    assert all(item.id.startswith("work_") and len(item.id) == 69 for item in original.work_items)


def test_result_is_deeply_immutable_and_payload_copies_do_not_change_it():
    result = parse(proposal())
    with pytest.raises(FrozenInstanceError):
        result.summary = "Changed"
    with pytest.raises(FrozenInstanceError):
        result.work_items[0].objective = "Changed"
    with pytest.raises(TypeError):
        result.work_items[0].read_roots[0] = "."
    payload = result.to_dict()
    payload["work_items"][0]["read_roots"].append("private")
    payload["work_items"].clear()
    assert result.work_items[0].read_roots == ("backend",)
    assert len(result.to_work_items()) == 1


@pytest.mark.parametrize("extra", ["tools", "provider", "model", "owner_id", "authority", "api_key", "request_limit", "executor_id"])
@pytest.mark.parametrize("location", ["plan", "item"])
def test_model_cannot_supply_authority_or_change_tool_model_or_budget(location, extra):
    value = proposal()
    (value if location == "plan" else value["work_items"][0])[extra] = "credential-sentinel"
    with pytest.raises(PlanRejected) as caught:
        parse(value)
    assert "credential-sentinel" not in str(caught.value)
    assert extra not in str(caught.value)


@pytest.mark.parametrize("residue", ["\n</function>\n</tool_call>", "<|im_end|>", " </tool_call> <|eot_id|>\n",
                                     "\n\n</function=bash>\n</function>\n</tool_call>"])
def test_chat_template_residue_after_the_json_is_dropped(residue):
    # A live model ended its final report with "</function></tool_call>".
    assert len(parse(json.dumps(proposal()) + residue).work_items) == 1
    with pytest.raises(PlanRejected):
        parse(json.dumps(proposal()) + residue + "\nThat is my plan.")


def test_planning_input_echoed_beside_the_plan_is_ignored():
    # A live model copied coordinator_read_roots from its input into the plan.
    # Echoes carry no meaning; other extra fields still refuse the plan (above).
    result = parse(proposal(coordinator_read_roots=["."], worker_slots=8, read_roots=["."]))
    assert [item.read_roots for item in result.work_items] == [("backend",)]


@pytest.mark.parametrize("field", ["id", "objective", "role", "dependencies", "read_roots", "write_roots", "criteria"])
def test_every_contract_dimension_must_be_explicit(field):
    item = work()
    del item[field]
    with pytest.raises(PlanRejected):
        parse(proposal([item]))


@pytest.mark.parametrize("root", ["../outside", "backend/../private", "/etc", "C:/outside", "C:relative", "\\\\host\\share",
                                  "backend/*.py", "backend:stream", "backend/NUL", "backend/file.", "backend/file "])
def test_scope_escape_and_ambiguous_paths_are_rejected(root):
    with pytest.raises(PlanRejected):
        parse(proposal([work(read_roots=[root])]))


@pytest.mark.parametrize("roots", [["."], ["backend-secret"], ["backend", "private"], ["backend2"]])
def test_outside_policy_is_rejected_not_silently_clipped(roots):
    with pytest.raises(PolicyDenied):
        parse(proposal([work(read_roots=roots)]), policy=replace(POLICY, read_roots=("backend",)))


def test_nested_scope_normalizes_safely_and_keeps_selected_provider():
    result = parse(proposal([work(read_roots=["backend\\csv\\./reader"])]), policy=replace(POLICY, read_roots=("backend/csv",)))
    assert result.work_items[0].read_roots == ("backend/csv/reader",)
    with pytest.raises(PolicyDenied):
        parse(proposal(), model=ModelSelection("sonn", "sonn-auto"))


def test_writer_scope_cannot_expand_to_parent_even_with_allowed_write_tools():
    with pytest.raises(PolicyDenied):
        parse(proposal([work(role="implement", write_roots=["backend"], criteria=["csv_acceptance"])]),
              policy=replace(POLICY, write_roots=("backend/csv",)))


def test_whitespace_only_criterion_is_invalid_even_if_supplied_in_trusted_catalog():
    with pytest.raises(PlanRejected):
        parse(proposal([work(criteria=[" "])]), allowed_criteria=frozenset({" "}))


@pytest.mark.parametrize("roots", [["backend", "backend"], ["backend", "backend/child"], ["backend", "backend/."]])
def test_duplicate_alias_or_redundant_scope_does_not_hide_parent_access(roots):
    with pytest.raises(PlanRejected):
        parse(proposal([work(read_roots=roots)]))


@pytest.mark.parametrize("criteria", [[], ["invented_check"], ["owner_review", "invented_check"], ["owner_review", "owner_review"]])
def test_criteria_are_nonempty_unique_and_selected_from_trusted_input(criteria):
    with pytest.raises(PlanRejected):
        parse(proposal([work(criteria=criteria)]))


@pytest.mark.parametrize("changes", [
    {"role": "explore", "write_roots": ["backend"]},
    {"role": "verify", "write_roots": ["backend"]},
    {"role": "implement", "write_roots": [], "criteria": ["csv_acceptance"]},
    {"role": "implement", "write_roots": ["backend"], "criteria": ["owner_review"]},
    {"role": "implement", "write_roots": ["backend"], "criteria": ["owner_review", "csv_acceptance"]},
    {"role": "supervisor"},
])
def test_role_and_criteria_cannot_convert_human_review_into_writer_acceptance(changes):
    with pytest.raises(PlanRejected):
        parse(proposal([work(**changes)]))


def test_tools_are_derived_from_role_and_policy_without_shell_or_provider_fields():
    limited = replace(POLICY, allowed_tools=frozenset({"file_read", "file_edit", "swarm_send", "bash", "task"}))
    result = parse(proposal([work(role="implement", write_roots=["backend"], criteria=["csv_acceptance"])]), policy=limited)
    assert result.work_items[0].tools == ("file_edit", "file_read", "swarm_send")
    with pytest.raises(PlanRejected):
        parse(proposal(), policy=replace(POLICY, allowed_tools=frozenset({"swarm_send"})))
    with pytest.raises(PlanRejected):
        parse(proposal([work(role="implement", write_roots=["backend"], criteria=["csv_acceptance"])]),
              policy=replace(POLICY, allowed_tools=FILE_TOOL_NAMES))


def test_empty_read_scope_remains_empty_and_does_not_gain_default_workspace_access():
    result = parse(proposal([work(read_roots=[])]), policy=replace(POLICY, read_roots=()))
    assert result.work_items[0].read_roots == ()


@pytest.mark.parametrize("items", [
    [work("a"), work("a")], [work("a", dependencies=["absent"])],
    [work("a", dependencies=["a"])], [work("a", dependencies=["b"]), work("b", dependencies=["a"])],
    [work("a"), work("b", dependencies=["a", "a"])],
])
def test_duplicate_missing_and_cyclic_dependencies_are_rejected(items):
    with pytest.raises(PlanRejected):
        parse(proposal(items))


def test_graph_node_count_is_not_concurrent_worker_count():
    items = [work(str(index), objective="Inspect", dependencies=[str(index-1)] if index else []) for index in range(256)]
    result = parse(proposal(items, summary="A bounded dependency chain"), policy=replace(POLICY, max_workers=1))
    assert len(result.work_items) == 256
    items.append(work("256", objective="Inspect"))
    with pytest.raises(PlanRejected):
        parse(proposal(items))
    with pytest.raises(PlanRejected):
        parse(proposal([]))


@pytest.mark.parametrize("wrapper", ["{}", "```json\n{}\n```", "```\n{}\n```", " \n{}\n "])
def test_only_one_json_object_with_optional_single_fence_is_accepted(wrapper):
    assert parse(wrapper.format(json.dumps(proposal()))).work_items


@pytest.mark.parametrize("raw", [
    'Here is the plan: {}', '{} trailing', '{} {}', '```json\n{}\n```\nExplanation',
    '```json\n{}\n```\n```json\n{}\n```', '```python\n{}\n```',
    '[]', 'null', 'true', '{"summary":"a","summary":"b","use_team":false,"work_items":[]}',
    '{"summary":NaN}', '{"summary":Infinity}', '{"summary":-Infinity}', '{"summary":1e999}',
])
def test_prose_duplicate_fields_nonfinite_numbers_and_trailing_data_are_rejected(raw):
    with pytest.raises(PlanRejected):
        parse(raw)


@pytest.mark.parametrize("changes", [{"use_team": 1}, {"use_team": "true"}, {"summary": ""}, {"summary": None}, {"work_items": {}}])
def test_no_implicit_type_coercions(changes):
    with pytest.raises(PlanRejected):
        parse(proposal(**changes))


def test_byte_limit_unicode_surrogates_and_nested_json_fail_closed():
    raw = json.dumps(proposal(), ensure_ascii=False)
    assert parse(raw + " " * (65536 - len(raw.encode()))).work_items
    with pytest.raises(PlanRejected):
        parse(raw + " " * (65537 - len(raw.encode())))
    with pytest.raises(PlanRejected):
        parse(proposal(summary="é" * 4097))
    with pytest.raises(PlanRejected):
        parse(proposal([work(read_roots=["\ud800"])]))
    with pytest.raises(PlanRejected):
        parse("[" * 1500 + "]" * 1500)


def test_parser_does_not_mutate_supplied_policy_or_input_contracts():
    value = proposal()
    before = json.dumps(value)
    policy_before = POLICY.to_dict()
    parse(value)
    assert json.dumps(value) == before and POLICY.to_dict() == policy_before


def test_prose_around_exactly_one_json_fence_is_read():
    # Live models often explain first; one fenced block is still unambiguous.
    fenced = "```json\n" + json.dumps(proposal()) + "\n```"
    assert parse("I read both files first.\n\n" + fenced + "\n\nThat is the plan.").use_team
    with pytest.raises(PlanRejected):
        parse("Two options:\n" + fenced + "\nor\n" + fenced)
    with pytest.raises(PlanRejected):
        parse("No JSON here, only prose.")


def test_only_follow_ups_and_owner_granted_orchestrators_may_propose_no_work():
    finished = proposal([], use_team=False, summary="Both defects found; see the evidence.")
    with pytest.raises(PlanRejected):
        parse(finished)
    assert parse(finished, allow_no_work=True).work_items == ()
    assert parse(finished, namespace="attempt-2").work_items == ()
