# Dictation

Talk instead of typing in the message box. The words land in the message
box for you to read and edit; dictation never sends anything.

## Dictating

- **Hold to talk.** Hold the microphone button beside the message box, or
  press and hold Ctrl+Shift+Space anywhere in the conversation, and talk.
  Let go to stop.
- **Or press once.** A quick press (or Enter on the button) keeps listening
  through pauses until you press again.
- **Escape cancels.** Nothing is added, and the message box keeps what it
  had.
- Switching projects or conversations cancels dictation before saving the
  old draft. Sending a message discards any pending transcription. A late
  result cannot add words to a different draft or the next message.
- The line under the message box says what's happening: listening,
  transcribing, or why dictation can't start. Screen readers announce it.
- Dictation stops after five minutes. It also stops after a long silence,
  when this window's speech recognition is listening.

The microphone button stays in the tab order even when dictation can't run
here. Pressing it says what to change.

## What listens (Settings > Voice)

| Choice | What happens to your voice |
| --- | --- |
| **Automatically** (the default) | The transcription service, if one is set and your organization allows it; otherwise this window's speech recognition. |
| **This window's speech recognition** | The browser engine recognizes speech as you talk. Chrome and Edge send it to Google or Microsoft, Safari to Apple, and Lumi can't see where or what they keep. The desktop app on Windows usually has no working recognizer. |
| **A transcription service** | Lumi records while you talk and sends the recording when you stop. Lumi keeps no copy. |
| **Off** | The microphone button explains that dictation is off. |

**Transcription services**

- **OpenAI** uses the OpenAI key under Settings > Connections (or
  `OPENAI_API_KEY`). Transcription API usage is billed separately from a
  ChatGPT subscription. **Sign in with ChatGPT** uses your subscription for
  coding through Codex; it does not supply a transcription API key.
  See [OpenAI's billing explanation](https://help.openai.com/en/articles/9039756).
- **Any connection that speaks OpenAI's API** also works. It must offer
  `POST /audio/transcriptions`, which OpenAI-compatible and OpenAI Responses
  connections both can. For example, a Whisper server on your own network,
  such as speaches, faster-whisper-server or LocalAI.
  - Add it under Settings > Connections as an OpenAI-compatible connection
    with its base URL, such as `http://10.0.0.5:8000/v1`.
  - Then choose it under Settings > Voice.
  - The connection's key, headers, OAuth sign-in and client certificate
    apply as they do for chat.

**Other settings**

- **Model.** `whisper-1` if you leave it empty. OpenAI also offers
  `gpt-4o-mini-transcribe` and `gpt-4o-transcribe`; other services name
  their own.
- **Language.** A tag such as `en`, `en-GB` or `de-DE`. Empty uses your
  system's language for this window's recognizer, and lets the service
  detect the language. The service receives only the first part (`en`).

**Recordings**

- WebM or Ogg Opus, or MP4 on macOS, as the window records it.
- Up to about 10 MB, which is far more than five minutes of speech.

## What Lumi records

- **Usage.** Each transcription is a usage record, with the purpose
  `dictation`, the service (`openai` or `conn-<id>`) and the model. It has
  no cost: services bill dictation per minute, not per token, so Usage &
  cost counts it as unpriced.
- **The audit log** records `voice.transcription`:
  - the service and model;
  - the recording's size and type;
  - how many characters came back;
  - whether it worked, and how long it took.

  It never records the words or the audio. The words reach the audit log
  only if you send them as a message.

## For administrators

Organization policy locks these like any other setting (see the
[policy guide](enterprise-policy.md)):

An invalid or expired organization policy blocks both transcription services
and this window's speech recognition. Automatic mode cannot fall back to a
browser recognizer while that policy is blocked.

| Setting | Values |
| --- | --- |
| `voice.engine` | `auto`, `browser`, `service` or `off`. `off` turns dictation off for everyone. |
| `voice.service` | `openai`, or `conn-<id>` for a connection. |
| `voice.model` | The transcription model. |
| `voice.language` | A language tag, or empty. |

The policy's model rules apply to the transcription service too. The
service's model is checked as `openai:whisper-1` or `conn-<id>:<model>`
against `models.allowed` and `models.blocked`.

**Zero data retention**

`models.require_zero_retention` does two things:

- It turns off this window's speech recognition, because Lumi can't tell
  what that service keeps.
- It allows only a service that keeps no data: a provider listed in
  `zero_retention_providers`, or a connection marked zero retention.

**Examples**

To keep dictation on your own Whisper server:

```json
{
  "schema": "lumi.policy/v1",
  "organization": "Example Health",
  "settings": {
    "voice.engine": "service",
    "voice.service": "conn-whisper",
    "voice.model": "Systran/faster-whisper-small"
  },
  "models": {"require_zero_retention": true}
}
```

For that policy to work, each computer's `whisper` connection must be marked
zero retention.

To turn dictation off:

```json
{"schema": "lumi.policy/v1", "settings": {"voice.engine": "off"}}
```

**On macOS**, the first dictation asks for the microphone, or for speech
recognition, and each person allows it. A configuration profile can't grant
either for them.

## For developers

- **`lumi/voice.py`**:
  - which engines may listen (`status`, sent to the page as
    `settings._meta.voice`);
  - checking the settings (`validate`, used for the socket and for policy);
  - the request to the service (`transcribe`).
- **`static/voice_input.js`** holds the dictation logic; `app.js` wires it
  to the button, the composer and the socket (`voice_transcribe`, answered
  by `voice.transcript` or `voice.error`).
- **Tests**:
  - `tests/test_voice.py`: the service, using `httpx.MockTransport`, never a
    real service;
  - `tests/voice_input.test.cjs`: the dictation logic, with a fake
    recognizer, microphone and clock.
