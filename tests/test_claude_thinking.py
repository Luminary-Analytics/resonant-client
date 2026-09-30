"""Claude's thinking levels in the shape each model takes, and its thinking sent back as the API requires.

Request shapes are checked against the API's own rules for each model, which
tests/api_provider_stub.py writes out from Anthropic's documentation. Its
scripted Messages API server refuses what the real API refuses: a thinking
budget from current models, more output than a model gives, a signed thinking
block not sent back exactly where the response had it, and, as for accounts
created on or after 2026-08-31, one bound to a conversation that has since
changed. No real key or provider is used.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from lumi import anthropic_api
from lumi import policy as lumi_policy
from lumi.anthropic_api import (
    BEDROCK_VERSION,
    VERTEX_VERSION,
    AnthropicBackend,
    block_order,
    encode_event_frame,
    request_prefix,
    text_digest,
)
from lumi.backends import EVENT_BACKEND_STATUS, EVENT_DONE, EVENT_ERROR, EVENT_TOOL_CALL, ChosenMaxTokens
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
LEVELS_ALL = ("default", "off", "low", "med", "high", "max")
UNBOUND = "bound to a different conversation"
ARN = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/a1b2c3d4e5f6"


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


def signatures(body: dict) -> list[str]:
    return [block.get("signature") or block.get("data") for block in thinking_in(body)]


def thinking_fields(body: dict) -> tuple:
    return body.get("thinking"), (body.get("output_config") or {}).get("effort")


# ── Which family a model id belongs to, on every platform ─────────────────


@pytest.mark.parametrize(("model", "family"), [
    ("claude-opus-5-5", "opus-5.5"),
    ("anthropic.claude-opus-5-5", "opus-5.5"),                                  # Bedrock
    ("claude-sonnet-5-5", "sonnet-5.5"),
    ("claude-fable-5-1", "fable-5.1"),
    ("claude-fable-5", "fable-mythos"),
    ("claude-mythos-5-1", "fable-mythos"),
    ("claude-mythos-preview", "mythos-preview"),
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
    ("claude-opus-6", "newer"),                                                 # newer than Lumi knows
    ("claude-haiku-5", "newer"),
    ("claude-sonnet-5-7", "newer"),
    ("claude-sonnet-4-7", "unrecognized"),                                      # not newer, not known
    ("corp-claude", "unrecognized"),                                            # a gateway's own name
    (ARN, "unrecognized"),                                                      # an application profile
    ("arn:aws:bedrock:us-east-1:123456789012:provisioned-model/abc123", "unrecognized"),
    ("glm-4.6", "unrecognized"),
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
# (thinking field, effort, max_tokens) for each level; a request at a level
# asks for room for its thinking (no max_tokens asked: 16,384).
LEVELS = {"low": (ADAPTIVE, "low", 16384), "med": (ADAPTIVE, "medium", 16384),
          "high": (ADAPTIVE, "high", 32000), "max": (ADAPTIVE, "max", 64000)}
BUDGETS = {mode: ({"type": "enabled", "budget_tokens": budget}, None, max(16384, budget + 4096))
           for mode, budget in {"low": 2048, "med": 6144, "high": 12288, "max": 24576}.items()}
NOTHING = (None, None, 16384)
LOWEST = (None, "low", 16384)


def shapes(default, off, levels):
    return {"default": default, "off": off, **levels}


SHAPES = {
    # Thinks without being asked and can't stop: "off" is its lowest effort.
    "claude-opus-5-5": shapes(NOTHING, LOWEST, LEVELS),
    "claude-fable-5-1": shapes(NOTHING, LOWEST, LEVELS),
    "claude-fable-5": shapes(NOTHING, LOWEST, LEVELS),
    # Thinks without being asked; "off" is between_tools, its lowest setting.
    "claude-sonnet-5-5": shapes(NOTHING, ({"type": "between_tools"}, None, 16384), LEVELS),
    # Thinks without being asked; "disabled" would stop it, but then Opus 5
    # writes tool calls as text and Sonnet 5 reaches for tools less: "off" is
    # the lowest effort, as Anthropic advises for work with tools.
    "claude-opus-5": shapes(NOTHING, LOWEST, LEVELS),
    "claude-sonnet-5": shapes(NOTHING, LOWEST, LEVELS),
    # Thinks when asked, adaptively.
    "claude-opus-4-8": shapes(NOTHING, NOTHING, LEVELS),
    "claude-opus-4-7": shapes(NOTHING, NOTHING, LEVELS),
    "claude-opus-4-6": shapes(NOTHING, NOTHING, LEVELS),
    "claude-sonnet-4-6": shapes(NOTHING, NOTHING, LEVELS),
    # A fixed budget only.
    "claude-opus-4-5-20251101": shapes(NOTHING, NOTHING, BUDGETS),
    "claude-sonnet-4-5-20250929": shapes(NOTHING, NOTHING, BUDGETS),
    "claude-haiku-4-5-20251001": shapes(NOTHING, NOTHING, BUDGETS),
    "claude-opus-4-1-20250805": shapes(NOTHING, NOTHING, BUDGETS),
    # No thinking, and at most 8,192 tokens out.
    "claude-3-5-haiku-20241022": {mode: (None, None, 8192) for mode in LEVELS_ALL},
}


def test_the_shapes_cover_every_model_the_api_rules_describe():
    assert set(SHAPES) == set(CLAUDE_RULES)


@pytest.mark.parametrize(("model", "mode"), [(model, mode) for model in SHAPES for mode in SHAPES[model]])
def test_each_thinking_level_goes_out_in_the_shape_the_model_takes(model, mode):
    payload = AnthropicBackend(KEY, model, thinking=mode)._payload("Plan the change", [], "Be brief.", TOOLS, None)
    thinking, effort, max_tokens = SHAPES[model][mode]
    assert thinking_fields(payload) == (thinking, effort)
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


def test_mythos_preview_takes_a_thinking_budget():
    # Anthropic's only example for it asks for a budget ("Before (Mythos
    # Preview / older models)" in the Fable 5.1 migration guide).
    payload = AnthropicBackend(KEY, "claude-mythos-preview", thinking="high")._payload("Plan", [], "", [], None)
    assert thinking_fields(payload) == ({"type": "enabled", "budget_tokens": 12288}, None)
    assert AnthropicBackend(KEY, "claude-mythos-preview")._payload("Plan", [], "", [], 32)["max_tokens"] == 32


# ── Room for thinking in max_tokens ───────────────────────────────────────


@pytest.mark.parametrize(("model", "mode", "max_tokens"), [
    ("claude-opus-5-5", "default", 16384),           # always thinks: Lumi's usual length at least
    ("claude-sonnet-5", "default", 16384),           # thinks without being asked
    ("claude-opus-4-8", "default", 32),              # thinks only when asked
    ("claude-sonnet-5", "off", 16384),               # thinks at the lowest effort
    ("claude-sonnet-5-5", "off", 32),                # no thinking before the answer
    ("claude-haiku-4-5-20251001", "low", 6144),      # a 2,048-token budget and room to answer
    ("claude-opus-5-5", "max", 64000),               # Anthropic suggests 64K at the highest efforts
])
def test_a_short_request_that_may_think_gets_room_for_the_thinking(model, mode, max_tokens):
    # A session title asks for 32 tokens, and thinking counts toward max_tokens.
    payload = AnthropicBackend(KEY, model, thinking=mode)._payload("Name this", [], "Title it.", [], 32)
    assert payload["max_tokens"] == max_tokens


def test_bedrock_keeps_its_thinking_room_within_what_lumi_asked_for_before():
    # Bedrock deducts max_tokens from the tokens-per-minute quota when a
    # request starts: at max, four participants would otherwise hold 4 x 64K.
    backend = AnthropicBackend("bedrock-key", "anthropic.claude-opus-5-5", platform="bedrock", region="us-east-1",
                               thinking="max")
    payload = backend._payload("Plan", [], "", TOOLS, None)
    assert thinking_fields(payload) == (ADAPTIVE, "max") and payload["max_tokens"] == 32000


def test_an_organization_can_say_a_model_has_no_thinking_levels():
    lumi_policy.set_for_tests(parse({"schema": "lumi.policy/v1", "models": {"capabilities": {
        "claude-opus-5*": {"reasoning": False}}}}, source="t"))
    payload = AnthropicBackend(KEY, "claude-opus-5-5", thinking="max")._payload("Hi", [], "", [], 32)
    assert "thinking" not in payload and "output_config" not in payload
    # It still thinks, so a short request keeps room for that.
    assert payload["max_tokens"] == 16384


# ── A limit the person set ─────────────────────────────────────────────────


def test_a_limit_the_person_set_stands_and_they_are_told_once(providers):
    backend = AnthropicBackend(KEY, "claude-opus-5-5", base_url=providers.url)
    first = list(backend.stream("Hi", [], "Be brief.", [], ChosenMaxTokens(4096)))
    notices = [data for kind, data in first if kind == EVENT_BACKEND_STATUS and data.get("kind") == "output_limit"]
    assert notices == [{
        "kind": "output_limit", "model": "claude-opus-5-5", "limit": 4096, "needed": 16384,
        "message": "claude-opus-5-5 thinks within the output limit you set (4,096 tokens), and at this thinking "
                   "level Lumi would allow it 16,384. Your limit stands, so an answer may stop early."}]
    second = list(backend.stream("Hi again", [], "Be brief.", [], ChosenMaxTokens(4096)))
    assert not [data for kind, data in second if kind == EVENT_BACKEND_STATUS and data.get("kind") == "output_limit"]
    assert [call["body"]["max_tokens"] for call in providers.of("anthropic")] == [4096, 4096]
    # Lumi's own small limits are still raised (a title asks for 32).
    assert backend._payload("Name this", [], "", [], 32)["max_tokens"] == 16384
    # A limit with room to spare says nothing, and neither does one on a model that doesn't think.
    assert AnthropicBackend(KEY, "claude-opus-5-5")._payload("Hi", [], "", [], ChosenMaxTokens(20000))[
        "max_tokens"] == 20000
    quiet = AnthropicBackend(KEY, "claude-opus-4-8", base_url=providers.url)
    assert not [kind for kind, data in quiet.stream("Hi", [], "", [], ChosenMaxTokens(512))
                if kind == EVENT_BACKEND_STATUS and data.get("kind") == "output_limit"]


def test_a_thinking_budget_fits_below_a_limit_the_person_set():
    haiku = AnthropicBackend(KEY, "claude-haiku-4-5-20251001", thinking="high")
    payload = haiku._payload("Plan", [], "", [], ChosenMaxTokens(8192))
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 4096} and payload["max_tokens"] == 8192
    assert haiku._short_limit == (8192, 16384)
    assert claude_refusal(CLAUDE_RULES["claude-haiku-4-5-20251001"], payload) == ""
    # No room for the smallest budget and an answer: no thinking.
    small = haiku._payload("Plan", [], "", [], ChosenMaxTokens(4096))
    assert "thinking" not in small and small["max_tokens"] == 4096


# ── The default level: main's request, byte for byte, where nothing needs to change ──

# What origin/main (c071cdf) sent at the default level for a tool loop's
# second request (a user message, a call made after signed thinking, its
# result), for a model id "MODEL". On main the body at the default level
# depends on the model only through its "model" field.
MAIN_DEFAULT_BODIES = {
    "direct": (
        '{"model": "MODEL", "max_tokens": 16384, "messages": [{"role": "user", "content": [{"type": "text", '
        '"text": "Read a.py"}]}, {"role": "assistant", "content": [{"type": "thinking", "thinking": "", '
        '"signature": "sig-1"}, {"type": "text", "text": "Reading."}, {"type": "tool_use", "id": "toolu_1", '
        '"name": "file_read", "input": {"path": "a.py"}}]}, {"role": "user", "content": [{"type": "tool_result", '
        '"tool_use_id": "toolu_1", "content": "print(\'a\')", "cache_control": {"type": "ephemeral"}}]}], '
        '"stream": true, "system": [{"type": "text", "text": "Be brief.", "cache_control": {"type": "ephemeral"}}], '
        '"tools": [{"name": "file_read", "description": "Read a file.", "input_schema": {"type": "object", '
        '"properties": {"path": {"type": "string"}}, "required": ["path"]}, "cache_control": {"type": '
        '"ephemeral"}}]}'),
    "bedrock": (
        '{"max_tokens": 16384, "messages": [{"role": "user", "content": [{"type": "text", "text": "Read a.py"}]}, '
        '{"role": "assistant", "content": [{"type": "thinking", "thinking": "", "signature": "sig-1"}, {"type": '
        '"text", "text": "Reading."}, {"type": "tool_use", "id": "toolu_1", "name": "file_read", "input": {"path": '
        '"a.py"}}]}, {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": '
        '"print(\'a\')", "cache_control": {"type": "ephemeral"}}]}], "system": [{"type": "text", "text": '
        '"Be brief.", "cache_control": {"type": "ephemeral"}}], "tools": [{"name": "file_read", "description": '
        '"Read a file.", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, '
        '"required": ["path"]}, "cache_control": {"type": "ephemeral"}}], "anthropic_version": '
        '"bedrock-2023-05-31"}'),
    "vertex": (
        '{"max_tokens": 16384, "messages": [{"role": "user", "content": [{"type": "text", "text": "Read a.py"}]}, '
        '{"role": "assistant", "content": [{"type": "thinking", "thinking": "", "signature": "sig-1"}, {"type": '
        '"text", "text": "Reading."}, {"type": "tool_use", "id": "toolu_1", "name": "file_read", "input": {"path": '
        '"a.py"}}]}, {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": '
        '"print(\'a\')", "cache_control": {"type": "ephemeral"}}]}], "stream": true, "system": [{"type": "text", '
        '"text": "Be brief.", "cache_control": {"type": "ephemeral"}}], "tools": [{"name": "file_read", '
        '"description": "Read a file.", "input_schema": {"type": "object", "properties": {"path": {"type": '
        '"string"}}, "required": ["path"]}, "cache_control": {"type": "ephemeral"}}], "anthropic_version": '
        '"vertex-2023-10-16"}'),
}
CALL = {"id": "toolu_1", "type": "function", "function": {"name": "file_read", "arguments": '{"path": "a.py"}'}}
# (platform, model id, base URL, thinks without being asked, largest max_tokens it takes). The two
# changes a family needs at the default level: a model that thinks anyway
# gets at least Lumi's usual 16,384 (a title's 32 would end inside its
# thinking), and no model is asked for more than it gives (main asked Claude
# 3.5 for 16,384; it gives 8,192).
DEFAULT_LEVEL = [
    ("direct", "claude-opus-5-5", "", True, 128_000),
    ("direct", "claude-fable-5-1", "", True, 128_000),
    ("direct", "claude-fable-5", "", True, 128_000),
    ("direct", "claude-mythos-5-1", "", True, 128_000),
    ("direct", "claude-sonnet-5-5", "", True, 128_000),
    ("direct", "claude-opus-5", "", True, 128_000),
    ("direct", "claude-sonnet-5", "", True, 128_000),
    ("direct", "claude-opus-4-8", "", False, 128_000),
    ("direct", "claude-opus-4-7", "", False, 128_000),
    ("direct", "claude-opus-4-6", "", False, 128_000),
    ("direct", "claude-sonnet-4-6", "", False, 128_000),
    ("direct", "claude-opus-4-5-20251101", "", False, 64_000),
    ("direct", "claude-sonnet-4-5-20250929", "", False, 64_000),
    ("direct", "claude-haiku-4-5-20251001", "", False, 64_000),
    ("direct", "claude-opus-4-1-20250805", "", False, 32_000),
    ("direct", "claude-sonnet-4-20250514", "", False, 64_000),
    ("direct", "claude-3-7-sonnet-20250219", "", False, 64_000),
    ("direct", "claude-mythos-preview", "", False, 32_000),
    ("direct", "claude-3-5-haiku-20241022", "", False, 8_192),
    ("direct", "claude-3-haiku-20240307", "", False, 4_096),
    ("direct", "claude-opus-6", "", True, 32_000),                      # newer than the table
    ("direct", "claude-neo", "", True, 32_000),                          # unknown, on the Anthropic API
    ("direct", "corp-claude", "https://gateway.example", False, 32_000),  # a gateway's own names
    ("direct", "glm-4.6", "https://gateway.example", False, 32_000),
    ("bedrock", "anthropic.claude-opus-5-5", "", True, 128_000),
    ("bedrock", "us.anthropic.claude-sonnet-4-5-20250929-v1:0", "", False, 64_000),
    ("bedrock", ARN, "", False, 32_000),
    ("vertex", "claude-sonnet-5-5", "", True, 128_000),
    ("vertex", "claude-haiku-4-5@20251001", "", False, 64_000),
    ("vertex", "claude-3-5-haiku@20241022", "", False, 8_192),
]


def platform_backend(platform: str, model: str, base_url: str = "", **options) -> AnthropicBackend:
    if platform == "bedrock":
        return AnthropicBackend("bedrock-key", model, platform="bedrock", region="us-east-1", **options)
    if platform == "vertex":
        return AnthropicBackend("", model, platform="vertex", region="us-east5", project="acme-ai",
                                access_token=lambda: "token", **options)
    return AnthropicBackend("key", model, base_url=base_url, **options)


@pytest.mark.parametrize("limit", [None, 20, 32, 1024, 4096, 16384])
@pytest.mark.parametrize(("platform", "model", "base_url", "thinks", "max_output"), DEFAULT_LEVEL)
def test_the_default_level_sends_mains_request_where_nothing_needs_to_change(platform, model, base_url, thinks,
                                                                             max_output, limit):
    main = json.loads(MAIN_DEFAULT_BODIES[platform])
    # The response's digest, as this adapter records it: the tool loop is
    # unchanged, so a model that binds its thinking gets it back too.
    prefix = request_prefix({"system": main["system"], "tools": main["tools"], "messages": main["messages"][:1]})
    history = [
        {"role": "user", "content": "Read a.py"},
        {"role": "tool_call", "name": "file_read", "arguments": '{"path": "a.py"}', "call_id": "toolu_1",
         "assistant_content": "Reading.", "response_id": "msg_1", "response_tool_calls": [CALL],
         "reasoning_details": [{"type": "thinking", "thinking": "", "signature": "sig-1", "provider": "anthropic"},
                               {"type": "replay", "provider": "anthropic", "prefix": prefix}],
         "provider_model": model},
        {"role": "tool_result", "call_id": "toolu_1", "content": "print('a')"},
    ]
    body = platform_backend(platform, model, base_url)._payload("", history, "Be brief.", TOOLS, limit)
    expected = dict(main)
    if "model" in expected:
        expected["model"] = model
    expected["max_tokens"] = min(max(limit or 16384, 16384 if thinks else 0), max_output)
    assert json.dumps(body, ensure_ascii=False) == json.dumps(expected, ensure_ascii=False)
    # Where nothing needs to change, that is main's request exactly.
    if expected["max_tokens"] == min(limit or 16384, 32000):
        assert json.dumps(expected, ensure_ascii=False) == MAIN_DEFAULT_BODIES[platform].replace("MODEL", model) \
            .replace('"max_tokens": 16384', f'"max_tokens": {expected["max_tokens"]}')


# ── Bedrock and Vertex AI ─────────────────────────────────────────────────


@pytest.mark.parametrize(("platform", "model", "mode", "thinking", "effort", "max_tokens"), [
    ("bedrock", "anthropic.claude-opus-5-5", "max", ADAPTIVE, "max", 32000),
    ("bedrock", "anthropic.claude-opus-5-5", "default", None, None, 16384),
    ("bedrock", "us.anthropic.claude-haiku-4-5-20251001-v1:0", "high",
     {"type": "enabled", "budget_tokens": 12288}, None, 16384),
    ("bedrock", "global.anthropic.claude-opus-4-6-v1", "off", None, None, 16384),
    ("vertex", "claude-sonnet-5-5", "off", {"type": "between_tools"}, None, 16384),
    ("vertex", "claude-sonnet-5-5", "default", None, None, 16384),
    ("vertex", "claude-opus-4-5@20251101", "med", {"type": "enabled", "budget_tokens": 6144}, None, 16384),
    ("vertex", "claude-fable-5-1", "off", None, "low", 16384),
    ("vertex", "claude-opus-5-5", "max", ADAPTIVE, "max", 64000),
])
def test_bedrock_and_vertex_bodies_carry_the_same_thinking_fields(platform, model, mode, thinking, effort,
                                                                  max_tokens):
    body = platform_backend(platform, model, thinking=mode)._payload("Plan the change", [], "Be brief.", TOOLS, None)
    assert thinking_fields(body) == (thinking, effort)
    assert body["max_tokens"] == max_tokens
    assert "model" not in body
    assert body["anthropic_version"] == (BEDROCK_VERSION if platform == "bedrock" else VERTEX_VERSION)


def bedrock_transport(real_model: str, refused: list[str], bodies: list[dict]) -> httpx.MockTransport:
    """Bedrock's InvokeModel stream, checking each body with ``real_model``'s rules."""
    def chunk(event: dict) -> bytes:
        return encode_event_frame({":event-type": "chunk", ":message-type": "event"}, json.dumps(
            {"bytes": base64.b64encode(json.dumps(event).encode()).decode()}).encode())

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        refusal = claude_refusal(CLAUDE_RULES[real_model], {**body, "model": real_model})
        if refusal:
            refused.append(refusal)
            return httpx.Response(400, headers={"x-amzn-errortype": "ValidationException:http://internal/"},
                                  json={"message": refusal})
        return httpx.Response(200, headers={"content-type": "application/vnd.amazon.eventstream"}, content=b"".join(
            chunk(event) for event in (
                {"type": "message_start", "message": {"id": "m", "usage": {"input_tokens": 4}}},
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Done."}},
                {"type": "message_delta", "delta": {}, "usage": {"output_tokens": 2}},
                {"type": "message_stop"})))
    return httpx.MockTransport(handler)


@pytest.mark.parametrize("real_model", ["claude-haiku-4-5-20251001", "claude-sonnet-4-5-20250929",
                                        "claude-opus-4-5-20251101", "claude-opus-4-8", "claude-opus-5-5"])
def test_an_application_inference_profile_is_sent_no_thinking_field_at_any_level(real_model):
    # Its ARN doesn't say which Claude model it serves, and a level sent in
    # the wrong family's shape is a 400: none is sent, at every level, and
    # nothing is added to max_tokens. Main sent a budget, which the newer
    # models behind it refuse; the newest shape, which the older ones refuse.
    refused: list[str] = []
    bodies: list[dict] = []
    for mode in LEVELS_ALL:
        backend = AnthropicBackend("bedrock-key", ARN, platform="bedrock", region="us-east-1", thinking=mode,
                                   transport=bedrock_transport(real_model, refused, bodies))
        assert list(backend.stream("Plan the change", [], "Be brief.", TOOLS))[-1][0] == EVENT_DONE
        assert list(backend.stream("Name this", [], "Title it.", [], 32))[-1][0] == EVENT_DONE
    assert refused == []
    assert {thinking_fields(body) for body in bodies} == {(None, None)}
    assert [body["max_tokens"] for body in bodies] == [16384, 32] * len(LEVELS_ALL)
    # It is still Claude: vision, tools and Claude's context window.
    profile = AnthropicBackend("bedrock-key", ARN, platform="bedrock", region="us-east-1").capability_profile
    assert profile.supports("vision") and profile.native_tools and profile.context_window == 200_000
    assert profile.reasoning_levels == ()


# ── Ids Lumi doesn't recognize ─────────────────────────────────────────────


def test_a_model_newer_than_the_table_gets_the_newest_shape_and_goes_on_without_it_if_refused(providers):
    # Pretend a newer model refused adaptive thinking: the request goes once
    # more without the thinking fields, and later requests go without them.
    providers.claude_rules["claude-opus-6"] = CLAUDE_RULES["claude-haiku-4-5-20251001"]
    backend = AnthropicBackend(KEY, "claude-opus-6", base_url=providers.url, thinking="high")
    assert list(backend.stream("Plan the change", [], "Be brief.", []))[-1][0] == EVENT_DONE
    assert list(backend.stream("And then?", [], "Be brief.", []))[-1][0] == EVENT_DONE
    first, again, later = (call["body"] for call in providers.of("anthropic"))
    assert thinking_fields(first) == (ADAPTIVE, "high") and first["max_tokens"] == 32000
    assert thinking_fields(again) == (None, None) == thinking_fields(later)
    assert providers.refused == ['"thinking.type.adaptive" is not supported for this model.']
    # Its "off" is the newest family's too: the lowest effort.
    off = AnthropicBackend(KEY, "claude-opus-6", thinking="off")._payload("Plan", [], "", [], None)
    assert thinking_fields(off) == (None, "low")


def test_an_unknown_name_on_the_anthropic_api_gets_the_newest_shape():
    # The Anthropic API serves only Claude: a name Lumi doesn't know there is
    # most likely a new model.
    backend = AnthropicBackend("key", "claude-neo", thinking="high")
    assert thinking_fields(backend._payload("Plan", [], "", [], None)) == (ADAPTIVE, "high")
    assert backend.capability_profile.reasoning_levels == ("low", "med", "high", "max")


@pytest.mark.parametrize("model", ["glm-4.6", "deepseek-chat", "kimi-k2-0905-preview", "MiniMax-M2", "gpt-4o",
                                   "corp-claude"])
def test_a_gateways_own_model_names_keep_their_capabilities_and_get_no_thinking_fields(providers, model):
    # An Anthropic-compatible endpoint (LiteLLM, a provider's own) may serve
    # models that aren't Claude: their names keep the capabilities they had,
    # the connection's settings apply, and no thinking field goes out at any
    # level, nor anything added to max_tokens.
    bodies = []
    for mode in LEVELS_ALL:
        backend = AnthropicBackend(KEY, model, base_url=providers.url, thinking=mode)
        bodies += [backend._payload("Plan the change", [], "Be brief.", TOOLS, None),
                   backend._payload("Name this", [], "Title it.", [], 32)]
    assert {thinking_fields(body) for body in bodies} == {(None, None)}
    assert [body["max_tokens"] for body in bodies] == [16384, 32] * len(LEVELS_ALL)
    assert list(backend.stream("Plan the change", [], "Be brief.", TOOLS))[-1][0] == EVENT_DONE
    assert providers.refused == [] and providers.errors == []
    assert backend.capability_profile == infer_model_capabilities(model)
    overridden = AnthropicBackend(KEY, model, base_url=providers.url,
                                  capability_overrides={"context_window": 128000, "modalities": ("text",)})
    assert overridden.capability_profile.context_window == 128000
    assert not overridden.capability_profile.supports("vision")


def test_a_gateway_models_thinking_goes_back_as_it_came(providers):
    # Not Claude, so no preserved-thinking check: its signed thinking goes
    # back in the tool loop even after the system prompt changed.
    providers.script = scripted(Reply(thinking="Look first.", tool=("file_read", {"path": "a.py"})),
                                Reply(text="It prints a."))
    backend = AnthropicBackend(KEY, "glm-4.6", base_url=providers.url)
    events = list(backend.stream("What does a.py print?", [], "Be brief.", TOOLS))
    history = history_of(events, "What does a.py print?", {'{"path": "a.py"}': "print('a')"})
    assert list(backend.stream("", history, "Be briefer.", TOOLS))[-1][0] == EVENT_DONE
    assert signatures(providers.of("anthropic")[1]["body"]) == ["sig-1"]


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


# ── Streaming thinking, and sending it back ───────────────────────────────

# Reasoning with the default display (no text, a signature), a remark, a
# progress note before each of two calls (one of them redacted).
INTERLEAVED = (("thinking", ""), ("text", "Checking both files."), ("thinking", "Reading a.py, then b.py."),
               ("tool", ("file_read", {"path": "a.py"})), ("redacted", ""), ("tool", ("file_read", {"path": "b.py"})))
BOTH = {'{"path": "a.py"}': "print('a')", '{"path": "b.py"}': "print('b')"}


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
    # Every thinking block, the empty one included, where each sat, and the
    # text they sat among.
    assert details == [
        {"type": "thinking", "thinking": "", "signature": "sig-1", "provider": "anthropic"},
        {"type": "thinking", "thinking": "Reading a.py, then b.py.", "signature": "sig-1.1", "provider": "anthropic"},
        {"type": "redacted_thinking", "data": "redacted-1.2", "provider": "anthropic"},
        {"type": "replay", "provider": "anthropic", "order": [["thinking", 0], ["text", 20], ["thinking", 1],
                                                              ["tool_use", 0], ["thinking", 2], ["tool_use", 1]],
         "text_digest": text_digest("Checking both files.")},
    ]
    assert calls[0]["reasoning_content"] == "Reading a.py, then b.py."

    history = history_of(events, "What do a.py and b.py print?", BOTH)
    assert list(backend.stream("", history, "Be brief.", TOOLS))[-1][0] == EVENT_DONE
    assert providers.errors == [] and providers.refused == []
    assistant = providers.of("anthropic")[1]["body"]["messages"][1]
    assert assistant["content"] == providers.issued["sig-1"]["content"]  # exactly as it came


@pytest.mark.parametrize("model", ["claude-opus-4-8", "claude-opus-5-5"])
@pytest.mark.parametrize("redacted", ["[redacted] both files.", "Checking both fileX."])  # a new length, the same
def test_a_turn_whose_text_was_redacted_goes_back_without_its_thinking(providers, model, redacted):
    # An organization's rules (or the secret scan) changed the text between
    # the thinking blocks: they can't go back where they sat, and anywhere
    # else the API refuses them, so the turn goes back without its thinking.
    providers.script = scripted(Reply(blocks=INTERLEAVED), Reply(text="a.py prints a; b.py prints b."))
    backend = AnthropicBackend(KEY, model, base_url=providers.url, thinking="high")
    events = list(backend.stream("What do a.py and b.py print?", [], "Be brief.", TOOLS))
    history = [{**entry, "assistant_content": redacted} if entry["role"] == "tool_call" else entry
               for entry in history_of(events, "What do a.py and b.py print?", BOTH)]
    assert list(backend.stream("", history, "Be brief.", TOOLS))[-1][0] == EVENT_DONE
    assert providers.refused == [] and providers.errors == []
    assistant = providers.of("anthropic")[1]["body"]["messages"][1]["content"]
    assert [block["type"] for block in assistant] == ["text", "tool_use", "tool_use"]
    assert assistant[0]["text"] == redacted


def test_the_thinking_a_model_does_without_being_asked_goes_back_too(providers):
    # Claude Opus 5.5, Lumi's default model, thinks at the default level too:
    # the scripted API answers with signed thinking it wasn't asked for.
    providers.script = scripted(Reply(tool=("file_read", {"path": "a.py"})), Reply(text="It prints a."))
    backend = AnthropicBackend(KEY, "claude-opus-5-5", base_url=providers.url)
    events = list(backend.stream("What does a.py print?", [], "Be brief.", TOOLS))
    first = providers.of("anthropic")[0]["body"]
    assert thinking_fields(first) == (None, None) and first["max_tokens"] == 16384
    call = next(data for kind, data in events if kind == EVENT_TOOL_CALL)
    assert call["reasoning_details"][0]["signature"] == "sig-1"
    history = history_of(events, "What does a.py print?", {'{"path": "a.py"}': "print('a')"})
    assert list(backend.stream("", history, "Be brief.", TOOLS))[-1][0] == EVENT_DONE
    assert signatures(providers.of("anthropic")[1]["body"]) == ["sig-1"]
    assert providers.refused == [] and providers.errors == []


def test_the_usual_block_order_records_nothing():
    assert block_order([{"type": "thinking"}, {"type": "text", "text": "Reading."}, {"type": "tool_use"}]) is None
    assert block_order([{"type": "text", "text": "Hi."}, {"type": "tool_use"}]) is None


# ── Preserved thinking ─────────────────────────────────────────────────────


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
    assert signatures(calls[3]) == ["sig-3"]
    assert calls[4]["system"] == calls[3]["system"]
    assert signatures(calls[4]) == ["sig-3"]


def test_a_block_the_api_says_is_bound_elsewhere_stays_out_and_the_request_goes_again(tmp_path, providers,
                                                                                         monkeypatch):
    # Without Lumi's own check, the API refuses the next turn. Sending the same
    # body again never clears it (the claude-api skill's error guide): the
    # named block and every thinking block after it stay out, the request goes
    # once more, and later requests leave them out from the start.
    from lumi.engine.session import Session

    monkeypatch.setattr(anthropic_api, "bound_thinking", lambda system, tools, messages, prefixes: messages)
    (tmp_path / "a.py").write_text("print('a')\n")
    providers.script = scripted(Reply(blocks=(("thinking", ""), ("tool", ("file_read", {"path": "a.py"})))),
                                Reply(text="It prints a."), Reply(text="Because it says so."),
                                Reply(text="Yes."))
    session = Session(AnthropicBackend(KEY, "claude-opus-5-5", base_url=providers.url), auto_approve=True)
    session.project_path = str(tmp_path)
    list(session.run("What does a.py print?"))
    session.project_instructions = "Answer in one line."
    second = list(session.run("Why?"))
    third = list(session.run("Sure?"))
    assert not [event for event in second + third if event.get("event") == "error"]
    assert len(providers.refused) == 1 and UNBOUND in providers.refused[0]
    refused, again, later = (call["body"] for call in providers.of("anthropic")[2:])
    assert signatures(refused) == ["sig-1"] and signatures(again) == [] and signatures(later) == []


def run_steps(providers: ScriptedProviders, model: str, level: str, steps: list[dict]) -> list[list[str]]:
    """Run a tool loop step by step as a Session does; the thinking each request sent back."""
    providers.script = scripted(*(step["reply"] for step in steps))
    backend = AnthropicBackend(KEY, model, base_url=providers.url, thinking=level)
    history: list[dict] = []
    for step in steps:
        if step.get("turn"):
            # A turn's own message is kept; a nudge goes only with its request.
            history.append({"role": "user", "content": step["turn"]})
        events = list(backend.stream(step.get("turn") or step.get("nudge") or "", history,
                                     step.get("system", "Be brief."), step.get("tools", TOOLS)))
        assert events[-1][0] == EVENT_DONE, events[-1]
        calls = [data for kind, data in events if kind == EVENT_TOOL_CALL]
        for data in calls:
            history.append({"role": "tool_call", "name": data["name"], "arguments": data["arguments"],
                            "call_id": data["call_id"], **{key: data[key] for key in (
                                "assistant_content", "response_id", "response_tool_calls", "reasoning_details",
                                "provider_model")}})
        history += [{"role": "tool_result", "call_id": data["call_id"], "content": "ok"} for data in calls]
        if not calls and step.get("keep_text"):
            history.append({"role": "assistant", "content": step["keep_text"]})
    return [signatures(call["body"]) for call in providers.of("anthropic")]


def call(n: int) -> Reply:
    return Reply(blocks=(("thinking", ""), ("tool", ("file_read", {"path": f"f{n}.py"}))))


# What changes mid-turn in Lumi, and the thinking each request can still send back.
SCENARIOS = {
    # A one-off nudge (a repeated call, a promise to act): sent as that
    # request's message only, so the thinking made after it can't go back.
    "nudge": ([{"turn": "Read the files.", "reply": call(1)},
               {"nudge": "You called file_read with the same arguments twice.", "reply": call(2)},
               {"reply": call(3)}, {"reply": Reply(blocks=(("thinking", ""), ("text", "Done.")))}],
              [[], ["sig-1"], ["sig-1"], ["sig-1", "sig-3"]]),
    # search_tools loads another tool: what came before is bound to the old set.
    "search_tools": ([{"turn": "Find and read.", "reply": call(1)},
                      {"tools": TOOLS + [GLOB], "reply": call(2)},
                      {"tools": TOOLS + [GLOB], "reply": Reply(blocks=(("thinking", ""), ("text", "Done.")))}],
                     [[], [], ["sig-2"]]),
    # A hook adds each step's context to the system prompt.
    "hook context": ([{"turn": "Read the files.", "system": "Be brief.\n\nstep 1", "reply": call(1)},
                      {"system": "Be brief.\n\nstep 2", "reply": call(2)},
                      {"system": "Be brief.\n\nstep 3", "reply": Reply(blocks=(("thinking", ""), ("text", "Done.")))}],
                     [[], [], []]),
    # A promise continued: the kept text and the next call become one message.
    "promise": ([{"turn": "Fix it.", "reply": Reply(blocks=(("thinking", ""), ("text", "I'll fix it."))),
                  "keep_text": "I'll fix it."},
                 {"nudge": "You promised to change the files.", "reply": call(2)},
                 {"reply": Reply(blocks=(("thinking", ""), ("text", "Fixed.")))}],
                [[], [], []]),
    # Progress notes around a remark, one redacted: in place, then kept.
    "interleaved": ([{"turn": "Read both.", "reply": Reply(blocks=INTERLEAVED)}, {"reply": call(2)},
                     {"reply": Reply(blocks=(("thinking", ""), ("text", "Done.")))}],
                    [[], ["sig-1", "sig-1.1", "redacted-1.2"], ["sig-1", "sig-1.1", "redacted-1.2", "sig-2"]]),
}


@pytest.mark.parametrize(("model", "level"), [("claude-opus-5-5", "high"), ("claude-sonnet-5-5", "off"),
                                              ("claude-fable-5-1", "default")])
@pytest.mark.parametrize("scenario", list(SCENARIOS))
def test_what_changes_mid_turn_leaves_out_only_the_thinking_bound_to_it(providers, model, level, scenario):
    steps, replayed = SCENARIOS[scenario]
    assert run_steps(providers, model, level, steps) == replayed
    # The scripted API checks what Anthropic describes, not Lumi's own check:
    # it refused nothing Lumi sent.
    assert providers.refused == [] and providers.errors == []


@pytest.mark.parametrize("scenario", ["nudge", "search_tools", "hook context", "promise"])
def test_without_that_check_the_api_refuses_and_the_request_goes_again(providers, monkeypatch, scenario):
    # The same steps without Lumi's own check: the scripted API refuses a block
    # bound elsewhere, and each request still goes through once that block and
    # the ones after it are left out.
    monkeypatch.setattr(anthropic_api, "bound_thinking", lambda system, tools, messages, prefixes: messages)
    steps, _ = SCENARIOS[scenario]
    run_steps(providers, "claude-opus-5-5", "high", steps)
    assert providers.refused and all(UNBOUND in refusal for refusal in providers.refused)


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
    assert signatures(providers.of("anthropic")[1]["body"]) == replayed


# ── The scripted API's check, as Anthropic describes it ────────────────────

T_READ = {"name": "file_read", "description": "Read a file.", "input_schema": {"type": "object"}}
T_GLOB = {"name": "glob", "description": "Find files.", "input_schema": {"type": "object"}}


def tool_turn(n: int, signature: str) -> tuple[dict, dict]:
    use = {"type": "tool_use", "id": f"toolu_{n}", "name": "file_read", "input": {"path": f"f{n}.py"}}
    result = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"toolu_{n}", "content": "ok"}]}
    return {"role": "assistant", "content": [{"type": "thinking", "thinking": "", "signature": signature}, use]}, result


def test_the_scripted_api_binds_thinking_to_what_anthropic_says_it_binds(providers):
    body = {"model": "claude-opus-5-5", "system": [{"type": "text", "text": "Be brief."}], "tools": [T_READ, T_GLOB],
            "messages": [{"role": "user", "content": [{"type": "text", "text": "Read the files."}]}]}
    turns = [tool_turn(n, f"sig-{name}") for n, name in enumerate("zab")]
    for number, (assistant, result) in enumerate(turns):
        providers.issue({**body, "messages": body["messages"] + [m for turn in turns[:number] for m in turn]},
                        assistant["content"])
    everything = body["messages"] + [message for turn in turns for message in turn]

    def replay(messages=everything, **changes):
        return providers.replay_refusal({**body, "messages": messages, **changes})

    assert replay() == ""
    # What the check ignores: a string for one text block, whitespace at
    # either end, cache_control, the tools' order.
    assert replay(system="  Be brief.\n") == ""
    assert replay(tools=[T_GLOB, {**T_READ, "cache_control": {"type": "ephemeral"}}]) == ""
    # What it doesn't: the system prompt, a tool, an earlier message.
    assert UNBOUND in replay(system="Be concise.")
    assert UNBOUND in replay(tools=[T_READ])
    assert UNBOUND in replay(messages=[{"role": "user", "content": "Read one file."}] + everything[1:])
    # Blocks can go from the front of the history, not from the middle.
    for name, refused in (("sig-z", False), ("sig-a", True)):
        without = [{**message, "content": message["content"][1:]}
                   if isinstance(message["content"], list) and message["content"][0].get("signature") == name
                   else message for message in everything]
        assert (UNBOUND in replay(messages=without)) is refused, name


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
    assert infer_model_capabilities("claude-opus-5").reasoning_can_disable is False
    assert infer_model_capabilities("claude-sonnet-5-5").reasoning_can_disable is True
    assert infer_model_capabilities("claude-haiku-4-5-20251001").reasoning_levels == ("low", "med", "high", "max")
    assert infer_model_capabilities("claude-3-5-haiku-20241022").reasoning_levels == ()
    # An id Lumi can't place gets no levels: none would be sent.
    assert infer_model_capabilities("corp-claude").reasoning_levels == ()
    assert infer_model_capabilities(ARN).reasoning_levels == ()
