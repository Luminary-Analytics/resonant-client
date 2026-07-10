from resonant_client.engine.policies import (
    ExecutionPolicy,
    PolicyAction,
    PolicyRule,
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
