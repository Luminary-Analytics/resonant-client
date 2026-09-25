"""Images for chat models that can't see them: a vision model describes them (lumi/engine/image_descriptions.py)."""
from __future__ import annotations

import base64

from lumi import usage
from lumi.capabilities import ModelCapabilities
from lumi.content import build_user_content, text_fallback
from lumi.engine import image_descriptions
from lumi.engine.model_roles import ModelRoleRouter
from lumi.engine.session import Session
from lumi.usage import UsageLedger
from tests.streaming_stub import StreamingBackend, done, text_delta

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
DESCRIPTION = "A login form. Error text: Invalid password."


def text_only(backend):
    backend.capability_profile = ModelCapabilities(model=backend.model, context_window=128_000)
    return backend


def seeing(backend):
    backend.capability_profile = ModelCapabilities(model=backend.model, context_window=128_000,
                                                   modalities=("text", "image"))
    return backend


def session_with(chat, vision=None):
    session = Session(backend=chat)
    if vision is not None:
        session.model_role_router = ModelRoleRouter(
            {"vision": {"backend_type": "anthropic", "model": "claude-sonnet-5"}},
            backend_factory=lambda profile: vision)
    return session


def statuses(events):
    return [e for e in events if e.get("event") == "backend.status" and e.get("kind") == "images_described"]


class TestFinding:
    def test_attached_images_and_tool_screenshots_without_a_description(self):
        attached = {"type": "image", "media_type": "image/png", "data": "aaa"}
        screenshot = {"type": "base64", "media_type": "image/png", "data": "bbb"}
        described = {"type": "image", "data": "ccc", "description": "done"}
        history = [
            {"role": "user", "content": [attached, {"type": "text", "text": "look"}, described]},
            {"role": "tool_result", "call_id": "c1", "content": "ok", "image": screenshot},
            {"role": "assistant", "content": "fine"},
        ]
        assert image_descriptions.pending(history) == [attached, screenshot]

    def test_the_text_fallback_says_who_described_the_image(self):
        text = text_fallback({"type": "image", "name": "shot", "media_type": "image/png",
                              "description": DESCRIPTION, "described_by": "anthropic:claude-sonnet-5"})
        assert text == f"[Image: shot (image/png), described by anthropic:claude-sonnet-5]\n{DESCRIPTION}"


class TestInATurn:
    def test_a_vision_model_describes_the_image_once(self, tmp_path):
        ledger = UsageLedger(tmp_path / "usage")
        usage.set_for_tests(ledger)
        chat = text_only(StreamingBackend(name="ollama", model="deepseek-v4-flash:cloud",
                                          scripts=[[text_delta("It says invalid password."), done()],
                                                   [text_delta("Still that."), done()]]))
        vision = seeing(StreamingBackend(name="anthropic", model="claude-sonnet-5",
                                         events=[text_delta(DESCRIPTION), ("done", {"stats": {"input_tokens": 900,
                                                                                               "output_tokens": 40}})]))
        session = session_with(chat, vision)
        events = list(session.run("What does the screenshot say?", images=[(PNG, "image/png")]))

        [status] = statuses(events)
        assert "anthropic:claude-sonnet-5 described 1 image for deepseek-v4-flash:cloud" in status["message"]
        [call] = vision.stream_calls
        image_part, prompt = call["user_msg"]
        assert image_part["type"] == "image" and prompt["text"].startswith("Describe this image")
        [image] = [part for turn in session.conversation_history if isinstance(turn.get("content"), list)
                   for part in turn["content"] if part.get("type") == "image"]
        assert (image["description"], image["described_by"]) == (DESCRIPTION, "anthropic:claude-sonnet-5")
        # The description is kept: the next turn asks nothing more of the vision model.
        list(session.run("And now?"))
        assert vision.stream_count == 1
        assert [r["purpose"] for r in ledger.records() if r["model"] == "claude-sonnet-5"] == ["image_description"]

    def test_tool_screenshots_are_described_too(self):
        chat = text_only(StreamingBackend(scripts=[[text_delta("ok"), done()]]))
        vision = seeing(StreamingBackend(name="anthropic", model="claude-sonnet-5",
                                         events=[text_delta(DESCRIPTION), done()]))
        session = session_with(chat, vision)
        session.conversation_history.append({"role": "tool_result", "call_id": "c1", "name": "browser_screenshot",
                                             "content": "captured", "image": {
                                                 "type": "base64", "media_type": "image/png",
                                                 "data": base64.b64encode(PNG).decode()}})
        list(session.run("What's on the page?"))
        assert session.conversation_history[0]["image"]["description"] == DESCRIPTION

    def test_nothing_is_asked_without_a_vision_model_or_when_the_chat_model_sees(self):
        vision = seeing(StreamingBackend(events=[text_delta(DESCRIPTION), done()]))
        # No vision model configured: the chat model gets the usual notice.
        session = session_with(text_only(StreamingBackend(events=[text_delta("ok"), done()])))
        events = list(session.run("Look", images=[(PNG, "image/png")]))
        assert statuses(events) == []
        # A chat model that sees images gets them as they are.
        session = session_with(seeing(StreamingBackend(events=[text_delta("ok"), done()])), vision)
        list(session.run("Look", images=[(PNG, "image/png")]))
        assert vision.stream_count == 0

    def test_a_failed_description_leaves_the_notice(self):
        vision = seeing(StreamingBackend(events=[("error", {"message": "overloaded"})]))
        session = session_with(text_only(StreamingBackend(events=[text_delta("ok"), done()])), vision)
        events = list(session.run("Look", images=[(PNG, "image/png")]))
        [status] = statuses(events)
        assert "couldn't describe" in status["message"]
        assert image_descriptions.pending(session.conversation_history)  # one more try next time
        list(session.run("Again"))
        assert vision.stream_count == 2
        # After two failures the image keeps its notice and costs nothing more.
        assert image_descriptions.pending(session.conversation_history) == []
        list(session.run("Once more"))
        assert vision.stream_count == 2


class TestAdapters:
    def test_a_gateway_without_vision_gets_text_in_place_of_images(self):
        from lumi.connections import create_connection_backend, normalize_connection

        connection = normalize_connection({"name": "Gateway", "type": "openai-compatible",
                                           "base_url": "https://llm.example.com/v1", "models": ["m"], "vision": False})
        backend = create_connection_backend(connection, "m", "key")
        assert not backend._accepts_images()
        image = {"type": "image", "media_type": "image/png", "data": base64.b64encode(PNG).decode(),
                 "description": DESCRIPTION, "described_by": "anthropic:claude-sonnet-5"}
        history = [{"role": "user", "content": [image, {"type": "text", "text": "look"}]},
                   {"role": "tool_result", "call_id": "c1", "name": "browser_screenshot", "content": "captured",
                    "image": {"type": "base64", "media_type": "image/png", "data": image["data"],
                              "description": DESCRIPTION, "described_by": "anthropic:claude-sonnet-5"}}]
        messages = backend._messages(history, "system", "next")
        text = str(messages)
        assert "image_url" not in text
        assert text.count("described by anthropic:claude-sonnet-5") == 2 and DESCRIPTION in text

    def test_a_gateway_with_vision_still_gets_the_image(self):
        from lumi.connections import create_connection_backend, normalize_connection

        connection = normalize_connection({"name": "Gateway", "type": "openai-compatible",
                                           "base_url": "https://llm.example.com/v1", "models": ["m"]})
        backend = create_connection_backend(connection, "m", "key")
        assert backend._accepts_images()
        content = backend._api_content(build_user_content("look", [(PNG, "image/png")]))
        assert any(part.get("type") == "image_url" for part in content)

    def test_sonn_passes_a_screenshots_description_labelled_as_such(self):
        from lumi.sonn import SonnBackend

        backend = SonnBackend.__new__(SonnBackend)
        backend._capabilities = ModelCapabilities(model="sonn-auto", context_window=128_000)
        turn = {"role": "tool_result", "call_id": "c1", "name": "browser_screenshot", "content": "captured",
                "image": {"type": "base64", "media_type": "image/png", "data": "abc",
                          "description": DESCRIPTION, "described_by": "anthropic:claude-sonnet-5"}}
        messages = backend._messages([turn], "system", "next")
        [tool] = [message for message in messages if message.get("role") == "tool"]
        assert "described by anthropic:claude-sonnet-5" in tool["content"] and DESCRIPTION in tool["content"]
        assert "image_url" not in str(messages)
