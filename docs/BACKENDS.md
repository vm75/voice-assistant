# Backends and response modes

The conversation backend is selected independently of local wake detection, Whisper
recognition, Piper speech and persistent alerts. New config selects **Hermes**.

## Hermes

Enable an authenticated Hermes agent API on your server using its
[API instructions](https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server).
The server must be reachable from the voice machine and its desired toolsets must be
enabled for API requests. Configuring the remote server is a separate administrator action.

On the voice machine:

```bash
cd "$HOME/voice-assistant"
python3 scripts/configure-backend.py hermes
bash scripts/install-voice.sh
python3 scripts/voicectl.py health
python3 scripts/voicectl.py ask 'What is the capital of Japan?' --speak
```

Enter your own HTTP(S) agent base URL ending in `/v1`; the hidden key prompt stores
`base_url` and `api_key` privately in `~/.config/jeeves/hermes.json`. Rerunning credential
setup replaces that backend's file. Restart voice after changing/rotating credentials.
Point at the **agent**, not its underlying model provider. Hermes executes its existing
HA/search tools; no separate NUC model provider or search account is installed.

Voice context does not inherit a messaging conversation. The frontend prompt instructs
the agent to look up current entity names/aliases/areas before asking what a room-labelled
device is. The selected profile, tools, permissions and model behavior remain server-side.

## Native Home Assistant alternative

Create a long-lived access token for the intended user on your HA server (Profile →
Security), then save it privately on the voice machine:

```bash
python3 scripts/configure-backend.py homeassistant
python3 scripts/voicectl.py backend homeassistant
python3 scripts/voicectl.py health --wait 120
python3 scripts/voicectl.py ask 'What time is it?' --speak
```

Enter the HA server base URL without `/api`. Its token is separate from the Hermes
API key. The adapter calls `/api/conversation/process`, passes language/agent settings,
and carries the returned conversation ID. It has no implicit ESPHome satellite area
context: name rooms/devices explicitly. It does not inherit Hermes web search.

This backend uses the same standalone microphone pipeline and local scheduler. The
[optional LVA frontend/package](HOME_ASSISTANT.md) is not required. Configure exposed
entities/aliases in HA as appropriate for the selected HA conversation agent.

Return to Hermes:

```bash
python3 scripts/voicectl.py backend hermes
python3 scripts/voicectl.py health --wait 120
```

Selection validates configuration/credential presence and restarts voice. A live request
is still required to validate a remote token and behavior. No request is automatically
retried or forwarded to a different backend after uncertain failure.

## Replace Hermes with another agent

Create a private credential JSON containing its `base_url` and `api_key`, with mode
600. Then add a named entry to `backends` in private `voice.json`. Example:

```json
{
  "other_agent": {
    "type": "openai",
    "credentials_file": "~/.config/jeeves/other-agent.json",
    "model": "YOUR_AGENT_MODEL_ID",
    "timeout": 120
  }
}
```

The credential file's `base_url` must end in `/v1`. An optional `base_url` in the backend
entry overrides it. Omit Hermes-specific session headers unless the replacement supports
them. The endpoint must execute its own tools and return the final answer.

```bash
python3 scripts/voicectl.py backend other_agent
python3 scripts/voicectl.py health --wait 120
```

For another wire protocol, implement `ConversationBackend.respond(text) -> Reply` and
`reset()` in `jeeves/backends/`, then register its type in `create_backend`. Raise
`BackendError` with a safe-to-speak message. Never include credentials in errors/logs.
Test against a local fake server before live use. “Claw” products need not implement
the same protocol; no compatibility is assumed without checking their API.

## Private settings and identity

The generated `voice.json` contains the selected backend, audio/wake/STT settings,
control token and adapter entries. Keys remain in separately referenced credential
files. Default Hermes settings include model alias `hermes-agent`, timeout 120 seconds,
`X-Hermes-Session-Id` and a hostname-derived `X-Hermes-Session-Key`. The native HA adapter
uses `agent_id: home_assistant`, English and a 30-second timeout. These are editable
protocol defaults, not addresses tied to a particular deployment.

Give machines distinct hostnames before initial setup, or customize `session_key` per
satellite to keep their identities separate. One machine has one shared voice context;
there is no per-speaker identity. “New conversation” resets local context; remote
history/memory is governed by the agent and is not erased by that command.

## Built-in wake words

The installed openWakeWord models include `alexa`, `hey_jarvis`, `hey_mycroft`,
`hey_rhasspy` and `okay_nabu`. The default is `hey_jarvis`. Set `wake.model` in private
`~/.config/jeeves/voice.json` to the model name and restart `jeeves-voice.service`.
Use one frontend at a time; the optional HA-managed frontend has its own wake settings.

## Quiet and talkative modes

```bash
python3 scripts/voicectl.py mode quiet
python3 scripts/voicectl.py mode talkative
python3 scripts/voicectl.py mode
```

Voice equivalents are “enable quiet mode” / “be less chatty” and “enable talkative
mode” / “disable quiet mode”. Ask “what mode are you in” for an audible answer.

Quiet mode suppresses only confirmed successful action confirmations. Answers,
clarification and failures still speak. Wake cues and actual timer/alarm/reminder
sounds are unaffected. Enabling quiet mode is itself silent; returning to talkative
mode speaks confirmation. The setting persists in private config immediately, without
restart. New configurations are talkative until explicitly changed; quickstart selects quiet.

`Reply.response_type` is `action_success`, `answer`, `clarification`, `error` or `unknown`.
Local alerts use scheduler results, native HA uses response/failed-target metadata,
and generic agents are prompted for final JSON with `text` and `response_type` after
their normal tool execution. A command phrased as “tell me…” is still an answer, and
a mixed action/question must keep its answer audible. Unknown metadata stays audible;
replacement adapters should supply a category. Classification is not independent
physical-state proof: device success still depends on tool results.

## Local API and diagnostics

The control API is loopback-only and authenticated by private `control_token`. CLI tools
read it without printing it. No keys need to appear in command arguments or shell history.

| Route | Request / result |
|---|---|
| `GET /health` | Selected backend/mode, listener state and errors, no transcript |
| `GET /last-turn` | Last recognized text/reply, backend, category and playback flags; RAM-only, five-minute expiry |
| `POST /ask` | JSON `text` and optional boolean `speak` (default false) |
| `POST /audio` | Raw mono S16_LE PCM at 16kHz, at most 60 seconds; uses STT and speaks its reply |
| `GET /mode` | Current mode |
| `POST /mode` | JSON `{"mode":"quiet"}` or `{"mode":"talkative"}` |

```bash
python3 scripts/voicectl.py last-turn
python3 scripts/voicectl.py audio /path/to/mono-16bit-16000Hz.wav
```

`--speak` remains subject to mode. Intentional suppression is a successful CLI outcome;
actual playback failure is not. Text remains available even when speech is suppressed.
A text test proves routing, not hardware capture. Also perform the real wake/chime/voice
request test. See [architecture](ARCHITECTURE.md) for bounds, retention and limitations.

References: [Hermes API](https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server),
[HA conversation API](https://developers.home-assistant.io/docs/intent_conversation_api/).
