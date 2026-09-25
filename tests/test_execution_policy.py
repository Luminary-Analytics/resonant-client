import pytest

from lumi.engine.policies import (
    ExecutionPolicy,
    PolicyAction,
    PolicyRule,
    repository_rules,
)


def test_command_globs_support_allow_prompt_deny_rules():
    policy = ExecutionPolicy([
        PolicyRule(
            tool_pattern="bash",
            action="deny",
            arg_globs={"command": ["rm -rf *", "* --force *"]},
        ),
        PolicyRule(
            tool_pattern="bash",
            action="allow",
            arg_globs={"command": ["git status*", "python -m pytest*"]},
        ),
        PolicyRule(tool_pattern="bash", action="prompt"),
    ])

    assert policy.evaluate("bash", {"command": "RM -RF build"}) == PolicyAction.DENY
    assert policy.evaluate("bash", {"command": "git status --short"}) == PolicyAction.ALLOW
    assert policy.evaluate("bash", {"command": "npm install"}) == PolicyAction.PROMPT


def test_arg_globs_load_from_policy_json_shape():
    policy = ExecutionPolicy.from_rules([
        {
            "tool_pattern": "bash",
            "action": "allow",
            "arg_globs": {"command": "ruff check*"},
        },
        {"tool_pattern": "bash", "action": "deny"},
    ])

    assert policy.evaluate("bash", {"command": "ruff check ."}) == PolicyAction.ALLOW
    assert policy.evaluate("bash", {"command": "npm install"}) == PolicyAction.DENY


# ── Rules that can't be applied are refused when they load ─────────────


@pytest.mark.parametrize("rule, message", [
    (1, "must be an object"),
    ({"tool_pattern": 5}, "tool_pattern"),
    ({"tool_pattern": None}, "tool_pattern"),
    ({"action": "block"}, "action must be"),
    ({"action": "Deny"}, "action must be"),
    ({"action": ["deny"]}, "action must be"),
    ({"arg_patterns": "x"}, "arg_patterns must map"),
    ({"arg_patterns": None}, "arg_patterns must map"),
    ({"arg_patterns": {"command": 5}}, "arg_patterns must map"),
    ({"arg_patterns": {"command": "("}}, "isn't a valid regular expression"),
    ({"arg_globs": "x"}, "arg_globs must map"),
    ({"arg_globs": {"command": 5}}, "arg_globs must map"),
    ({"arg_globs": {"command": ["git push*", 5]}}, "arg_globs must map"),
])
def test_a_rule_that_cant_be_applied_is_refused_when_it_loads(rule, message):
    with pytest.raises(ValueError, match=message):
        PolicyRule.from_dict(rule)
    with pytest.raises(ValueError, match=f"rule 2: .*{message}"):
        ExecutionPolicy.from_rules([{"tool_pattern": "bash", "action": "deny"}, rule])


def test_rules_keep_their_defaults_and_ignore_keys_lumi_doesnt_use():
    rule = PolicyRule.from_dict({"tool_pattern": "bash", "comment": "anything", "reason": None})
    assert (rule.action, rule.arg_patterns, rule.arg_globs, rule.reason) == ("allow", {}, {}, "")
    assert PolicyRule.from_dict({}).tool_pattern == "*"


def test_a_repository_file_with_a_broken_rule_keeps_only_its_deny_and_prompt_rules():
    file_rules = [
        {"tool_pattern": "file_write", "action": "deny", "reason": "frozen"},
        {"tool_pattern": "bash", "action": "prompt", "arg_globs": {"command": "git push*"}},
        {"tool_pattern": "bash", "action": "deny", "arg_patterns": "rm"},
        {"tool_pattern": "bash", "action": "allow"},
    ]

    rules, problems = repository_rules({"rules": file_rules})

    assert problems == ["rule 3: arg_patterns must map argument names to regular expressions"]
    # First match wins: without rule 3, the allow after it could let through
    # what rule 3 was meant to refuse, so the file's allows are off too.
    assert [(rule.tool_pattern, rule.action) for rule in rules] == [("file_write", "deny"), ("bash", "prompt")]
    fixed, problems = repository_rules({"rules": file_rules[:2] + file_rules[3:]})
    assert problems == [] and [rule.action for rule in fixed] == ["deny", "prompt", "allow"]


@pytest.mark.parametrize("document, problem", [
    ([], 'JSON object with a "rules" list'),
    ([{"tool_pattern": "bash", "action": "deny"}], 'JSON object with a "rules" list'),
    ({"rules": "x"}, '"rules" must be a list'),
    ({"rules": 5}, '"rules" must be a list'),
    ({"rules": None}, '"rules" must be a list'),
    ({"rules": {"tool_pattern": "bash", "action": "deny"}}, '"rules" must be a list'),
])
def test_a_repository_file_without_a_rules_list_contributes_nothing(document, problem):
    rules, problems = repository_rules(document)
    assert rules == [] and len(problems) == 1 and problem in problems[0]


def test_a_repository_file_without_rules_has_no_mistakes():
    assert repository_rules({}) == ([], [])
    assert repository_rules({"rules": []}) == ([], [])
