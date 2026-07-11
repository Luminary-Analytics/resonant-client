import base64

from resonant_client.capabilities import (
    default_context_window,
    infer_model_capabilities,
)
from resonant_client.content import (
    build_user_content,
    content_text,
    normalize_content,
    ollama_message_content,
)


def test_flagship_profiles_expose_large_context_and_agentic_capabilities():
    glm = infer_model_capabilities("glm-5.2:cloud")
    deepseek = infer_model_capabilities("deepseek-v4-pro:cloud")

    assert glm.context_window == 999_424
    assert deepseek.context_window == 1_048_576
    assert glm.supports("tools") and glm.supports("parallel_tool_calls")
    assert deepseek.supports("reasoning")
    assert not glm.supports("structured_outputs")
    assert not deepseek.supports("structured_outputs")
    assert default_context_window("mystery:latest") == 32_768


def test_runtime_metadata_can_enable_future_modalities_without_model_branch():
    profile = infer_model_capabilities("future-coder:latest")
    enriched = profile.with_runtime_metadata(
        ["completion", "tools", "vision", "audio", "thinking"],
        context_window=262_144,
    )

    assert enriched.source == "reported"
    assert enriched.context_window == 262_144
    assert enriched.supports("image")
    assert enriched.supports("audio")
    assert enriched.supports("tools")
    assert enriched.supports("reasoning")
    assert enriched.to_dict()["modalities"] == ("text", "image", "audio")


def test_nonempty_runtime_capabilities_override_incorrect_vision_inference():
    inferred = infer_model_capabilities("qwen3-vl:future")
    assert inferred.supports("vision")

    reported = inferred.with_runtime_metadata(["completion", "tools"])
    assert not reported.supports("vision")


def test_user_images_are_normalized_without_changing_plain_text_history():
    assert build_user_content("plain", None) == "plain"

    image = b"fake-png"
    content = build_user_content("inspect this", [(image, "image/png")])
    assert content[0]["type"] == "image"
    assert base64.b64decode(content[0]["data"]) == image
    assert content[-1] == {"type": "text", "text": "inspect this"}


def test_text_only_model_receives_explicit_media_fallback_not_silent_drop():
    content = [
        {"type": "image", "media_type": "image/png", "data": "aGVsbG8="},
        {"type": "text", "text": "Find the visual defect."},
    ]

    text, images = ollama_message_content(content, allow_images=False)

    assert images == []
    assert "Image" in text
    assert "text-only" not in text  # capability wording stays adapter-neutral
    assert "No textual representation is available" in text
    assert "Find the visual defect" in text


def test_native_vision_keeps_image_and_optional_description():
    content = [
        {
            "type": "image",
            "media_type": "image/png",
            "data": "aGVsbG8=",
            "description": "A settings dialog with a clipped save button.",
        },
        {"type": "text", "text": "Fix it."},
    ]

    text, images = ollama_message_content(content, allow_images=True)

    assert images == ["aGVsbG8="]
    assert "clipped save button" in text
    assert "Fix it" in text


def test_unknown_parts_are_preserved_as_diagnostic_text():
    normalized = normalize_content([{"type": "point-cloud", "points": 42}])
    assert normalized[0]["type"] == "diagnostic"
    assert "unsupported:point-cloud" in content_text(normalized)
