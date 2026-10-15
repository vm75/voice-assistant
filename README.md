# Jeeves Voice Assistant

[![CI](https://github.com/vm75/voice-assistant/actions/workflows/ci.yml/badge.svg)](https://github.com/vm75/voice-assistant/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.13%20%7C%203.14-blue?logo=python&logoColor=white)](.github/workflows/ci.yml)
[![Platform](https://img.shields.io/badge/Platform-Ubuntu%20Server-E95420?logo=ubuntu&logoColor=white)](docs/QUICKSTART.md)
[![Local audio](https://img.shields.io/badge/Wake%20%2F%20STT%20%2F%20TTS-local-green)](docs/ARCHITECTURE.md)

Turn an Ubuntu machine and a USB Jabra speakerphone into a voice assistant. Say
**“Hey Jarvis”**, wait for the chime, then ask a question or give a command.

Jeeves handles wake detection, speech recognition and speech generation locally.
Conversation goes to **Hermes Agent** by default, reusing its configured Home Assistant
and web-search tools. Native Home Assistant conversation and other OpenAI-compatible
agents are selectable alternatives.

- **Audible local timers, alarms and reminders**, saved in SQLite and restored on restart.
- **Piper neural speech**, with a downloaded voice that works offline.
- **Quiet mode**: successful action confirmations are silent; answers, clarification
  and failures still speak. Wake chimes and actual scheduled alerts stay audible.
- **Modular backends**, separate from capture, recognition, playback and local alerts.
- **Boot startup without login**, using systemd user services and lingering.
- **Private configuration** outside the repository; no deployment address or key is bundled.

## Get started

You need Ubuntu Server (26.04 LTS x86_64 tested), a USB Jabra mic/speaker, internet for
installation, and a reachable authenticated Hermes agent API. The agent must already
have the tools you want to use. A desktop is not required.

```bash
sudo apt-get update
sudo apt-get install -y git python3
git clone https://github.com/vm75/voice-assistant.git "$HOME/voice-assistant"
cd "$HOME/voice-assistant"
```

Follow the [minimal setup guide](docs/QUICKSTART.md) to install audio, enter your own
Hermes endpoint/key, and start the services. Setup discovers the Jabra, uses the host's
timezone, and creates a hostname-based voice identity. No fixed username or LAN address
is assumed. For native HA mode, see [backend setup](docs/BACKENDS.md).

## Use it

After the wake chime, try:

- “What is the capital of Japan?”
- “Open the office blinds.”
- “Remind me to check the oven in ten minutes.”
- “Set an alarm for seven AM.”
- “Enable quiet mode.” / “Enable talkative mode.”

```bash
python3 scripts/voicectl.py health
python3 scripts/voicectl.py mode quiet
python3 scripts/voicectl.py ask 'What is the capital of Japan?' --speak
python3 scripts/alertctl.py dismiss
```

The initial response mode is talkative; the quickstart selects quiet mode. Settings
persist across restart and backend changes. Each follow-up needs the wake word again.

## Documentation

| Guide | Contents |
|---|---|
| [Quickstart](docs/QUICKSTART.md) | Fresh minimal Ubuntu + Jabra installation |
| [Architecture](docs/ARCHITECTURE.md) | Components, boundaries, interfaces and limits |
| [Backends](docs/BACKENDS.md) | Hermes, native HA, replacement agents and response modes |
| [Execution and recovery](docs/EXECUTION.md) | Reproducible commands, timezone/audio settings, backups and troubleshooting |
| [Optional HA-managed frontend](docs/HOME_ASSISTANT.md) | LVA/ESPHome and HA package setup |
| [Voice model attribution](docs/voices/LESSAC_MODEL_CARD.md) | Bundled voice metadata and training-data attribution |

## Development

The scheduler, adapters and control tests use only the Python standard library; they
need no microphone, API keys, network service or ML model download:

```bash
python3 -m unittest discover -s tests -v
python3 scripts/check-docs.py
```

ML dependencies live in separate pinned environments created by the installers. CI
checks Python 3.13/3.14, local HTTP integrations, shell syntax and documentation examples.

## Practical limits

General conversation, live search and home control depend on the selected agent/server
and its model/tool availability. Local scheduling and already saved alerts work without
that server after installation. An external agent may use cloud inference and retain
conversations according to its own settings.

English recognition, one shared conversation per machine, one-shot alarms and bounded
recordings are supported. Replies do not support barge-in or streaming playback yet.
Music integration, custom wake-word training and hardware-specific extras are separate
work; physical audio and cold-boot behavior should be tested on each new machine.
