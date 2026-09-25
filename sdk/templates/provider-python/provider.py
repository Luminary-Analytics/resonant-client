"""A Lumi model provider (Extension SDK v1): start here.

Lumi runs this file once per request (see "providers" in lumi-pack.json) and
talks to it over stdin and stdout; lumi_extension does the talking. It
offers two models:

* ``echo`` answers without a network, so the pack works as soon as it's
  installed. Say ``call <tool> {"argument": "value"}`` to see a tool call.
* ``remote`` forwards the conversation to an OpenAI-compatible Chat
  Completions endpoint. Set MY_PROVIDER_BASE_URL and MY_PROVIDER_MODEL (or
  edit them below), and give the connection in Lumi the endpoint's key:
  Lumi passes it to this process as LUMI_PROVIDER_API_KEY.

Replace either with your own model. See docs/extensions.md in the Lumi repository.
"""

import json
import os
import urllib.request

from lumi_extension import Provider, StreamRequest, done, error, serve, text, tool_call

BASE_URL = os.environ.get("MY_PROVIDER_BASE_URL", "https://api.example.com/v1").rstrip("/")
REMOTE_MODEL = os.environ.get("MY_PROVIDER_MODEL", "your-model-id")


class MyProvider(Provider):
    def models(self):
        return [{"id": "echo", "context_window": 32768, "tools": True},
                {"id": "remote", "context_window": 128000, "tools": True}]

    def stream(self, request: StreamRequest):
        if request.model == "echo":
            yield from echo(request)
        elif request.model == "remote":
            yield from remote(request)
        else:
            yield error(f"This provider has no model {request.model!r}.")


def echo(request):
    """Repeat the last message, call a tool when asked to, and report a tool's answer."""
    last = request.messages[-1] if request.messages else {"role": "user", "content": ""}
    words = str(last.get("content") or "").split(maxsplit=2)
    names = {tool.get("name") for tool in request.tools}
    if last["role"] == "user" and len(words) >= 2 and words[0] == "call" and words[1] in names:
        yield tool_call(words[1], json.loads(words[2]) if len(words) > 2 else {})
    elif last["role"] == "tool":
        yield text(f"The tool answered: {last['content'][:500]}")
    else:
        yield text(f"You said: {last.get('content') or ''}")
    size = sum(len(str(message.get("content") or "")) for message in request.messages)
    yield done(input_tokens=size // 4, output_tokens=12, cost_usd=0.0)


def remote(request):
    """One Chat Completions request; its answer becomes Lumi's events."""
    body = {"model": REMOTE_MODEL, "messages": to_openai(request.messages)}
    if request.tools:
        body["tools"] = [{"type": "function", "function": tool} for tool in request.tools]
    if request.max_tokens:
        body["max_tokens"] = request.max_tokens
    headers = {"Content-Type": "application/json"}
    if request.api_key:
        headers["Authorization"] = f"Bearer {request.api_key}"
    call = urllib.request.Request(f"{BASE_URL}/chat/completions", data=json.dumps(body).encode("utf-8"),
                                  headers=headers, method="POST")
    try:
        with urllib.request.urlopen(call, timeout=300) as response:
            answer = json.load(response)
    except OSError as exc:  # urllib's errors, refusals and timeouts are all OSErrors
        yield error(f"{BASE_URL} didn't answer: {exc}")
        return
    message = (answer.get("choices") or [{}])[0].get("message") or {}
    if message.get("content"):
        yield text(message["content"])
    for requested in message.get("tool_calls") or []:
        function = requested.get("function") or {}
        yield tool_call(function.get("name", ""), json.loads(function.get("arguments") or "{}"),
                        id=requested.get("id", ""))
    usage = answer.get("usage") or {}
    yield done(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))


def to_openai(messages):
    """Lumi's messages in the Chat Completions format."""
    result = []
    for message in messages:
        if message["role"] == "assistant" and message.get("tool_calls"):
            result.append({"role": "assistant", "content": message.get("content") or None, "tool_calls": [
                {"id": call["id"], "type": "function",
                 "function": {"name": call["name"], "arguments": json.dumps(call["arguments"])}}
                for call in message["tool_calls"]]})
        elif message["role"] == "tool":
            result.append({"role": "tool", "tool_call_id": message["tool_call_id"], "content": message["content"]})
        else:
            result.append({"role": message["role"], "content": message["content"]})
    return result


if __name__ == "__main__":
    raise SystemExit(serve(MyProvider()))
