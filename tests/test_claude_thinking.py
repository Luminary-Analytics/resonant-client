"""Claude's thinking levels in the shape each model takes, and its thinking sent back as the API requires.

Request shapes are checked against the API's own rules for each model, which
tests/api_provider_stub.py writes out from Anthropic's documentation. Its
scripted Messages API server refuses what the real API refuses: a thinking
budget from current models, a signed thinking block not sent back exactly
where the response had it, and, as for accounts created on or after
2026-08-31, one bound to a conversation that has since changed. No real key
or provider is used.
"""

from __future__ import annotations

import pytest

from lumi import anthropic_api
from lumi import policy as lumi_policy
from lumi.anthropic_api import BEDROCK_VERSION, VERTEX_VERSION, AnthropicBackend, block_order, request_prefix
from lumi.backends import EVENT_DONE, EVENT_ERROR, EVENT_TOOL_CALL
from lumi.capabilities import infer_model_capabilities
from lumi.claude_models import claude_model, normalize_model_id
from lumi.engine.model_roles import ModelRoleRouter
from lumi.gui.runtime import BackendSpec
from lumi.policy import parse
from tests.api_provider_stub import CLAUDE_RULES, KEY, Reply, ScriptedProviders, claude_refusal

TOOLS = [{"type": "function", "function": {
    "name": "file_read", "description": "Read a file.",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}]
GLOB = {"type": "function", "function": {
    "name": "glob", "description": "Find files.",
    "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}}}
THINKING = {"thinking", "redacted_thinking"}


@pytest.fixture
def providers():
    server = ScriptedProviders(enforce_prefix=True)
    yield server
    server.close()


def scripted(*replies):
    """Answer the requests in order."""
    queue = list(replies)
    return lambda turn: queue.pop(0)


def thinking_in(body: dict) -> list[dict]:
    return [block for message in body["messages"] if isinstance(message["content"], list)
            for block in message["content"] if block["type"] in THINKING]


# ── Which family a model id belongs to, on every platform ─────────────────


@pytest.mark.parametrize(("model", "family"), [
    ("claude-opus-5-5", "opus-5.5"),
    ("anthropic.claude-opus-5-5", "opus-5.5"),                                  # Bedrock
    ("claude-sonnet-5-5", "sonnet-5.5"),
    ("claude-fable-5-1", "fable-5.1"),
    ("claude-fable-5", "fable-mythos"),
    ("claude-mythos-5-1", "fable-mythos"),
    ("claude-mythos-preview", "fable-mythos"),
    ("claude-opus-5", "opus-5-sonnet-5"),
    ("us.anthropic.claude-sonnet-5-v1:0", "opus-5-sonnet-5"),                  # a Bedrock inference profile
    ("claude-opus-4-8", "adaptive-opt-in"),
    ("arn:aws:bedrock:us-east-1:123456789012:inference-profile/global.anthropic.claude-opus-4-6-v1",
     "adaptive-opt-in"),                                                        # its ARN
    ("claude-sonnet-4-6", "adaptive-opt-in"),
    ("claude-opus-4-5@20251101", "budget"),                                     # Vertex AI
    ("claude-haiku-4-5-20251001", "budget"),
    ("us.anthropic.claude-sonnet-4-5-20250929-v1:0", "budget"),
    ("claude-sonnet-4-20250514", "budget"),
    ("claude-3-7-sonnet-latest", "budget"),
    ("claude-3-5-haiku-20241022", "no-thinking"),
    ("claude-opus-6", "unrecognized"),                                          # newer than Lumi knows
    ("corp-claude", "unrecognized"),                                            # a gateway's own name
    ("arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/a1b2c3", "unrecognized"),
])
def test_every_platforms_model_id_finds_its_family(model, family):
    assert claude_model(model).family == family


def test_platform_ids_normalize_to_the_claude_apis_name():
    assert normalize_model_id("us.anthropic.claude-sonnet-4-5-20250929-v1:0") == "claude-sonnet-4-5"
    assert normalize_model_id("claude-sonnet-4-5@20250929") == "claude-sonnet-4-5"
    assert normalize_model_id("anthropic/claude-opus-5-5") == "claude-opus-5-5"
    assert normalize_model_id("anthropic/claude-sonnet-4.5") == "claude-sonnet-4-5"
    assert claude_model("anthropic/claude-3.5-sonnet").family == "no-thinking"
    assert claude_model("claude-opus-4-1-20250805").max_output == 32_000
    assert claude_model("claude-opus-6").known is False and claude_model("claude-opus-5-5").known


# ── The request shape for each family and level ───────────────────────────

ADAPTIVE = {"type": "adaptive"}
# (thinking field, effort, max_tokens) for each level; a request that may
# think asks for room for it (no max_tokens asked: 16,384).
LEVELS = {"low": (ADAPTIVE, "low", 16384), "med": (ADAPTIVE, "medium", 16384),
          "high": (ADAPTIVE, "high", 32000), "max": (ADAPTIVE, "max", 64000)}
BUDGETS = {mode: ({"type": "enabled", "budget_tokens": budget}, None, max(16384, budget + 4096))
           for mode, budget in {"low": 2048, "med": 6144, "high": 12288, "max": 24576}.items()}
NOTHING = (None, None, 16384)


def shapes(default, off, levels):
    return {"default": default, "off": off, **levels}


SHAPES = {
    # Thinks without being asked and can't stop: "off" is its lowest effort.
    "claude-opus-5-5": shapes((None, None, 16384), (None, "low", 16384), LEVELS),
    "claude-fable-5-1": shapes((None, None, 32000), (None, "low", 16384), LEVELS),
    "claude-fable-5": shapes((None, None, 32000), (None, "low", 16384), LEVELS),
    # Thinks without being asked; "off" is between_tools, its lowest setting.
    "claude-sonnet-5-5": shapes((None, None, 32000), ({"type": "between_tools"}, None, 16384), LEVELS),
    # Thinks without being asked; "off" disables it.
    "claude-opus-5": shapes((None, None, 32000), ({"type": "disabled"}, None, 16384), LEVELS),
    "claude-sonnet-5": shapes((None, None, 32000), ({"type": "disabled"}, None, 16384), LEVELS),
    # Thinks when asked, adaptively.
    "claude-opus-4-8": shapes(NOTHING, NOTHING, LEVELS),
    "claude-opus-4-7": shapes(NOTHING, NOTHING, LEVELS),
    "claude-opus-4-6": shapes(NOTHING, NOTHING, LEVELS),
    "claude-sonnet-4-6": shapes(NOTHING, NOTHING, LEVELS),
    # A fixed budget only.
    "claude-opus-4-5-20251101": shapes(NOTHING, NOTHING, BUDGETS),
    "claude-sonnet-4-5-20250929": shapes(NOTHING, NOTHING, BUDGETS),
    "claude-haiku-4-5-20251001": shapes(NOTHING, NOTHING, BUDGETS),
}


def test_the_shapes_cover_every_model_the_api_rules_describe():
    assert set(SHAPES) == set(CLAUDE_RULES)


@pytest.mark.parametrize(("model", "mode"), [(model, mode) for model in SHAPES for mode in SHAPES[model]])
def test_each_thinking_level_goes_out_in_the_shape_the_model_takes(model, mode):
    payload = AnthropicBackend(KEY, model, thinking=mode)._payload("Plan the change", [], "Be brief.", TOOLS, None)
    thinking, effort, max_tokens = SHAPES[model][mode]
    assert payload.get("thinking") == thinking
    assert (payload.get("output_config") or {}).get("effort") == effort
    assert payload["max_tokens"] == max_tokens
    # Never a parameter the model refuses: the API's own rules for it accept the request.
    assert claude_refusal(CLAUDE_RULES[model], payload) == ""
    assert not {"temperature", "top_p", "top_k"} & set(payload)


@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-fable-5",
                                   "claude-opus-5", "claude-sonnet-5", "claude-opus-4-8", "claude-opus-4-7"])
def test_a_thinking_budget_is_what_current_models_refuse(model):
    # The request Lumi used to send for every level but the default.
    old = {"model": model, "max_tokens": 16384, "thinking": {"type": "enabled", "budget_tokens": 12288},
           "messages": [{"role": "user", "content": [{"type": "text", "text": "Plan the change"}]}]}
    assert claude_refusal(CLAUDE_RULES[model], old) == (
        '"thinking.type.enabled" is not supported for this model. Use "thinking.type.adaptive" and '
        '"output_config.effort" to control thinking behavior.')


@pytest.mark.parametrize(("model", "mode", "max_tokens"), [
    ("claude-opus-5-5", "default", 16384),           # always thinks, at medium effort
    ("claude-sonnet-5", "default", 32000),           # thinks without being asked, at high effort
    ("claude-opus-4-8", "default", 32),              # thinks only when asked
    ("claude-sonnet-5", "off", 32),                  # thinking disabled
    ("claude-sonnet-5-5", "off", 32),                # no thinking before the answer
    ("claude-haiku-4-5-20251001", "low", 6144),      # a 2,048-token budget and room to answer
])
def test_a_short_request_that_may_think_gets_room_for_the_thinking(model, mode, max_tokens):
    # A session title asks for 32 tokens, and thinking counts toward max_tokens.
    payload = AnthropicBackend(KEY, model, thinking=mode)._payload("Name this", [], "Title it.", [], 32)
    assert payload["max_tokens"] == max_tokens


def test_an_organization_can_say_a_model_has_no_thinking_levels():
    lumi_policy.set_for_tests(parse({"schema": "lumi.policy/v1", "models": {"capabilities": {
        "corp-claude*": {"reasoning": False}}}}, source="t"))
    payload = AnthropicBackend(KEY, "corp-claude-haiku", thinking="max")._payload("Hi", [], "", [], None)
    assert "thinking" not in payload and "output_config" not in payload


@pytest.mark.parametrize(("platform", "model", "mode", "thinking", "effort", "max_tokens"), [
    ("bedrock", "anthropic.claude-opus-5-5", "max", ADAPTIVE, "max", 64000),
    ("bedrock", "us.anthropic.claude-haiku-4-5-20251001-v1:0", "high",
     {"type": "enabled", "budget_tokens": 12288}, None, 16384),
    ("bedrock", "global.anthropic.claude-opus-4-6-v1", "off", None, None, 16384),
    ("vertex", "claude-sonnet-5-5", "off", {"type": "between_tools"}, None, 16384),
    ("vertex", "claude-opus-4-5@20251101", "med", {"type": "enabled", "budget_tokens": 6144}, None, 16384),
    ("vertex", "claude-fable-5-1", "off", None, "low", 16384),
])
def test_bedrock_and_vertex_bodies_carry_the_same_thinking_fields(platform, model, mode, thinking, effort,
                                                                  max_tokens):
    extra = {"project": "acme-ai", "access_token": lambda: "ya29.token"} if platform == "vertex" else {}
    backend = AnthropicBackend("bedrock-key", model, platform=platform, region="us-east5", thinking=mode, **extra)
    body = backend._payload("Plan the change", [], "Be brief.", TOOLS, None)
    assert body.get("thinking") == thinking
    assert (body.get("output_config") or {}).get("effort") == effort
    assert body["max_tokens"] == max_tokens
    assert "model" not in body
    assert body["anthropic_version"] == (BEDROCK_VERSION if platform == "bedrock" else VERTEX_VERSION)


# ── What the API says, and what Lumi then says ─────────────────────────────


def test_the_api_refuses_the_budget_lumi_used_to_send_and_takes_what_it_sends_now(providers):
    before = AnthropicBackend(KEY, "claude-opus-5-5", base_url=providers.url, thinking="high")
    before._claude = claude_model("claude-haiku-4-5-20251001")  # every level was a fixed budget
    kind, data = list(before.stream("Plan the change", [], "Be brief.", []))[-1]
    assert kind == EVENT_ERROR and data["status_code"] == 400
    assert data["message"] == (
        "Anthropic refused the thinking settings Lumi sent for claude-opus-5-5 (a 12,288-token thinking "
        'budget): "thinking.type.enabled" is not supported for this model. Use "thinking.type.adaptive" and '
        '"output_config.effort" to control thinking behavior. Set this model\'s thinking level to the provider '
        "default.")

    now = AnthropicBackend(KEY, "claude-opus-5-5", base_url=providers.url, thinking="high")
    assert list(now.stream("Plan the change", [], "Be brief.", []))[-1][0] == EVENT_DONE
    refused, answered = providers.of("anthropic")
    assert refused["body"]["thinking"] == {"type": "enabled", "budget_tokens": 12288}
    assert answered["body"]["thinking"] == ADAPTIVE and answered["body"]["output_config"] == {"effort": "high"}
    assert len(providers.refused) == 1


def test_an_unrecognized_model_gets_the_newest_shape_and_a_clear_error_if_refused(providers):
    # A gateway's own name for what is in fact Claude Haiku 4.5, which takes only a budget.
    providers.claude_rules["corp-claude"] = CLAUDE_RULES["claude-haiku-4-5-20251001"]
    backend = AnthropicBackend(KEY, "corp-claude", base_url=providers.url, thinking="high")
    kind, data = list(backend.stream("Plan the change", [], "Be brief.", []))[-1]
    assert kind == EVENT_ERROR
    assert data["message"] == (
        "Anthropic refused the thinking settings for corp-claude (adaptive thinking at high effort): "
        '"thinking.type.adaptive" is not supported for this model. Lumi doesn\'t recognize this model, so it '
        "sent what the newest Claude models take. Set this model's thinking level to the provider default, or "
        "choose a Claude model Lumi knows.")
    # Its output limit is unknown too, so a request asks for no more than Lumi always did.
    assert providers.of("anthropic")[0]["body"]["max_tokens"] == 32000
    # At the provider default nothing is sent, and it answers.
    default = AnthropicBackend(KEY, "corp-claude", base_url=providers.url)
    assert list(default.stream("Plan the change", [], "Be brief.", []))[-1][0] == EVENT_DONE


# ── Streaming thinking, and sending it back ───────────────────────────────

# Reasoning with the default display (no text, a signature), a remark, a
# progress note before each of two calls (one of them redacted).
INTERLEAVED = (("thinking", ""), ("text", "Checking both files."), ("thinking", "Reading a.py, then b.py."),
               ("tool", ("file_read", {"path": "a.py"})), ("redacted", ""), ("tool", ("file_read", {"path": "b.py"})))


def history_of(events: list, prompt: str, results: dict[str, str]) -> list[dict]:
    """The conversation history a session keeps for a streamed tool-calling response."""
    history: list[dict] = [{"role": "user", "content": prompt}]
    for kind, data in events:
        if kind == EVENT_TOOL_CALL:
            history.append({"role": "tool_call", "name": data["name"], "arguments": data["arguments"],
                            "call_id": data["call_id"], **{key: data[key] for key in (
                                "assistant_content", "response_id", "response_tool_calls", "reasoning_details",
                                "provider_model")}})
    history += [{"role": "tool_result", "call_id": entry["call_id"], "content": results[entry["arguments"]]}
                for entry in history if entry["role"] == "tool_call"]
    return history


def test_streamed_thinking_keeps_every_block_and_where_it_sat(providers):
    providers.script = scripted(Reply(blocks=INTERLEAVED), Reply(text="a.py prints a; b.py prints b."))
    backend = AnthropicBackend(KEY, "claude-opus-4-8", base_url=providers.url, thinking="high")
    events = list(backend.stream("What do a.py and b.py print?", [], "Be brief.", TOOLS))
    calls = [data for kind, data in events if kind == EVENT_TOOL_CALL]
    assert [call["call_id"] for call in calls] == ["toolu_fixture_1", "toolu_fixture_1_1"]
    details = calls[0]["reasoning_details"]
    assert details == calls[1]["reasoning_details"]
    # Every thinking block, the empty one included, and where each sat.
    assert details == [
        {"type": "thinking", "thinking": "", "signature": "sig-1", "provider": "anthropic"},
        {"type": "thinking", "thinking": "Reading a.py, then b.py.", "signature": "sig-1.1", "provider": "anthropic"},
        {"type": "redacted_thinking", "data": "redacted-1.2", "provider": "anthropic"},
        {"type": "replay", "provider": "anthropic", "order": [["thinking", 0], ["text", 20], ["thinking", 1],
                                                              ["tool_use", 0], ["thinking", 2], ["tool_use", 1]]},
    ]
    assert calls[0]["reasoning_content"] == "Reading a.py, then b.py."

    history = history_of(events, "What do a.py and b.py print?",
                         {'{"path": "a.py"}': "print('a')", '{"path": "b.py"}': "print('b')"})
    assert list(backend.stream("", history, "Be brief.", TOOLS))[-1][0] == EVENT_DONE
    assert providers.errors == [] and providers.refused == []
    assistant = providers.of("anthropic")[1]["body"]["messages"][1]
    assert assistant["content"] == providers.issued["sig-1"]["content"]  # exactly as it came

    # A redaction (the organization's rules) changed the text: its old place no
    # longer fits, so the usual order goes out rather than a guess.
    edited = [{**entry, "assistant_content": "Checking."} if entry["role"] == "tool_call" else entry
              for entry in history]
    kinds = [block["type"] for block in backend._payload("", edited, "Be brief.", TOOLS, None)["messages"][1]["content"]]
    assert kinds == ["thinking", "thinking", "redacted_thinking", "text", "tool_use", "tool_use"]


def test_the_usual_block_order_records_nothing():
    assert block_order([{"type": "thinking"}, {"type": "text", "text": "Reading."}, {"type": "tool_use"}]) is None
    assert block_order([{"type": "text", "text": "Hi."}, {"type": "tool_use"}]) is None


def test_a_chat_on_claude_opus_5_5_sends_its_thinking_back_as_the_api_requires(tmp_path, providers):
    from lumi.engine.session import Session

    for name in "abc":
        (tmp_path / f"{name}.py").write_text(f"print('{name}')\n")
    providers.script = scripted(
        Reply(blocks=INTERLEAVED),
        Reply(blocks=(("thinking", ""), ("text", "a.py prints a; b.py prints b."))),
        Reply(blocks=(("thinking", ""), ("tool", ("file_read", {"path": "c.py"})))),
        Reply(blocks=(("thinking", ""), ("text", "c.py prints c."))),
        Reply(text="You're welcome."),
    )
    session = Session(AnthropicBackend(KEY, "claude-opus-5-5", base_url=providers.url, thinking="high"),
                      auto_approve=True)
    session.project_path = str(tmp_path)
    first = list(session.run("What do a.py and b.py print?"))
    # A later turn's system prompt differs: each turn's recalled notes, memory
    # and code go there.
    session.project_instructions = "Answer in one line."
    second = list(session.run("And c.py?"))
    third = list(session.run("Thanks."))

    assert providers.errors == [] and providers.refused == []
    assert not [event for event in first + second + third if event.get("event") == "error"]
    calls = [call["body"] for call in providers.of("anthropic")]
    assert len(calls) == 5
    for body in calls:
        assert body["thinking"] == ADAPTIVE and body["output_config"] == {"effort": "high"}
        assert body["max_tokens"] == 32000
    # The tool loop sends the response back exactly as it came, every block in its place.
    assert calls[1]["messages"][1]["content"] == providers.issued["sig-1"]["content"]
    # The first turn's thinking is bound to the old system prompt, so it stays out...
    assert calls[2]["system"] != calls[1]["system"] and thinking_in(calls[2]) == []
    # ...and the new turn's own thinking goes back within its tool loop, and on
    # the next turn, whose conversation before it is unchanged.
    assert [block["signature"] for block in thinking_in(calls[3])] == ["sig-3"]
    assert calls[4]["system"] == calls[3]["system"]
    assert [block["signature"] for block in thinking_in(calls[4])] == ["sig-3"]


def test_without_that_check_the_api_refuses_the_next_turn(tmp_path, providers, monkeypatch):
    from lumi.engine.session import Session

    monkeypatch.setattr(anthropic_api, "bound_thinking", lambda system, tools, messages, prefixes: messages)
    (tmp_path / "a.py").write_text("print('a')\n")
    providers.script = scripted(Reply(blocks=(("thinking", ""), ("tool", ("file_read", {"path": "a.py"})))),
                                Reply(text="It prints a."))
    session = Session(AnthropicBackend(KEY, "claude-opus-5-5", base_url=providers.url), auto_approve=True)
    session.project_path = str(tmp_path)
    list(session.run("What does a.py print?"))
    session.project_instructions = "Answer in one line."
    events = list(session.run("Why?"))
    error = next(event for event in events if event.get("event") == "error")
    assert "bound to a different conversation" in error["message"]
    assert len(providers.refused) == 1


@pytest.mark.parametrize(("tools", "replayed"), [(TOOLS, ["sig-1"]), (TOOLS + [GLOB], [])])
def test_thinking_goes_back_only_while_the_tools_it_saw_are_offered(providers, tools, replayed):
    # search_tools loads more tools mid-turn; thinking made before is bound to
    # the tools offered then (Claude Sonnet 5.5 checks it).
    providers.script = scripted(Reply(blocks=(("thinking", ""), ("tool", ("file_read", {"path": "a.py"})))),
                                Reply(text="It prints a."))
    backend = AnthropicBackend(KEY, "claude-sonnet-5-5", base_url=providers.url, thinking="med")
    events = list(backend.stream("What does a.py print?", [], "Be brief.", TOOLS))
    meta = next(data for kind, data in events if kind == EVENT_TOOL_CALL)["reasoning_details"][-1]
    assert meta["type"] == "replay" and meta["prefix"] == request_prefix(providers.of("anthropic")[0]["body"])
    history = history_of(events, "What does a.py print?", {'{"path": "a.py"}': "print('a')"})
    assert list(backend.stream("", history, "Be brief.", tools))[-1][0] == EVENT_DONE
    assert providers.errors == [] and providers.refused == []
    assert [block["signature"] for block in thinking_in(providers.of("anthropic")[1]["body"])] == replayed


# ── Models for roles ───────────────────────────────────────────────────────

PIXEL = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="


def test_a_claude_vision_role_describes_images_at_its_thinking_level(providers):
    # "vision anthropic:claude-sonnet-5" under Models for roles runs at the
    # vision role's level, high, which Claude Sonnet 5 refused as a budget.
    from lumi.engine.image_descriptions import describe

    providers.script = lambda turn: Reply(text="A single white pixel.")
    router = ModelRoleRouter({"vision": {"backend_type": "anthropic", "model": "claude-sonnet-5"}},
                             backend_factory=lambda profile: BackendSpec(
                                 profile.backend_type, profile.model, base_url=providers.url, api_key=KEY,
                                 thinking_mode=profile.thinking_mode).create_backend())
    backend = router.backend_for("vision", None)
    assert backend.thinking_mode == "high"
    assert describe(backend, {"media_type": "image/png", "data": PIXEL}) == "A single white pixel."
    body = providers.of("anthropic")[0]["body"]
    assert body["thinking"] == ADAPTIVE and body["output_config"] == {"effort": "high"}
    # Room for thinking beyond the description's 1,500 tokens.
    assert body["max_tokens"] == 32000
    assert providers.refused == []


# ── The capability catalog ────────────────────────────────────────────────


def test_the_catalog_says_which_claude_models_can_turn_thinking_off():
    assert infer_model_capabilities("claude-opus-5-5").reasoning_can_disable is False
    assert infer_model_capabilities("claude-fable-5-1").reasoning_can_disable is False
    assert infer_model_capabilities("claude-sonnet-5-5").reasoning_can_disable is True
    assert infer_model_capabilities("claude-haiku-4-5-20251001").reasoning_levels == ("low", "med", "high", "max")
    assert infer_model_capabilities("claude-3-5-haiku-20241022").reasoning_levels == ()
    # An inference profile ARN doesn't say it's Claude; on the Anthropic adapter it is.
    arn = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/a1b2c3"
    assert infer_model_capabilities(arn).reasoning_levels == ()
    profile = AnthropicBackend("bedrock-key", arn, platform="bedrock", region="us-east-1").capability_profile
    assert profile.reasoning_levels and profile.supports("vision") and profile.reasoning_can_disable is False
