# Installation, operations and recovery

Commands are run as a normal Ubuntu user with sudo access. The checkout can be under
any user/path; examples use `~/voice-assistant`. Secrets/configuration belong outside
git under `~/.config/jeeves/`. For the shortest install sequence, use [quickstart](QUICKSTART.md).

## Reproduce installation

```bash
sudo apt-get update
sudo apt-get install -y git python3
git clone https://github.com/vm75/voice-assistant.git "$HOME/voice-assistant"
cd "$HOME/voice-assistant"
bash scripts/install-alerts.sh
export PATH="$HOME/.local/bin:$PATH"
python3 scripts/configure-backend.py hermes
bash scripts/install-voice.sh
python3 scripts/voicectl.py mode quiet
python3 scripts/voicectl.py health
```

Enter the actual Hermes server URL/key at the interactive prompts. Existing backend
configuration is preserved, so a reinstall doesn't silently replace a selected backend.
The scripts log reproducible commands/output under ignored `logs/`; credentials are
entered through a separate hidden prompt and are not logged.

| Installer | Actions |
|---|---|
| `install-alerts.sh` | OS packages, audio group/ACLs, user lingering, PipeWire services, alert config/unit, Piper helper, alert startup |
| `install-tts.sh` | Pinned Python/Piper environment, checked model download, private speech configuration |
| `install-voice.sh` | Pinned voice environment, pinned STT download, private voice config/unit, frontend switch and startup check |
| `install-satellite.sh` | Optional LVA frontend installation, disabling standalone voice before enabling LVA |

Python environments use 3.13.16. Piper dependencies are pinned in
`locks/piper-requirements.txt` (Piper 1.8.0). Voice dependencies use
`locks/voice-requirements.txt` (faster-whisper and pyopen-wakeword). Installers bootstrap
the pinned uv utility if needed. Weights are downloaded during setup and loaded from
disk at runtime; Hugging Face offline mode is enabled for the voice service.

Piper voice: `en_US-lessac-high`, revision
`c10ece1aade47bb51c153c893d14e5bf8e5b7117` of `rhasspy/piper-voices`, verified against
`models/tts/SHA256SUMS`. [Voice attribution](voices/LESSAC_MODEL_CARD.md) stays in docs.
Whisper: `Systran/faster-whisper-base.en`, revision
`3d3d5dee26484f91867d81cb899cfcf72b96be6c`. Model binaries and runtime environments
are ignored by git and can be retained in backups or downloaded again.

## Timezone and audio selection

New alert config adopts the machine's existing IANA timezone (UTC fallback). The
installer does not change the host clock/timezone. Verify NTP/timezone with:

```bash
timedatectl
```

To intentionally change the OS timezone, supply your chosen IANA zone to
`sudo timedatectl set-timezone`. To set/update the assistant's timezone independently:

```bash
python3 scripts/configure.py --timezone Europe/London
systemctl --user restart jeeves-alerts
```

`Europe/London` is an example selection, not a project default. Use the zone appropriate
to your deployment. Keep OS and assistant zones aligned if wall-clock behavior should
match local system time. Explicit-offset ISO timestamps are supported by the alarm API.

Setup discovers one Jabra sink/source. For an explicit device selection:

```bash
pactl list short sinks
pactl list short sources
python3 scripts/configure.py --sink YOUR_SINK_NAME
python3 scripts/configure-voice.py --source YOUR_SOURCE_NAME
systemctl --user restart jeeves-alerts jeeves-voice
```

Use actual names from `pactl`; do not use ALSA card indices. Sink updates affect both
services after restart. The initial audio ACL enables testing before new `audio` group
membership takes effect on reboot. Before that first reboot, USB replug can require
rerunning the installer ACL command. Verify unplug/replug on the actual hardware.

## Services and diagnostics

```bash
systemctl --user is-enabled jeeves-voice jeeves-alerts
systemctl --user is-active pipewire pipewire-pulse wireplumber jeeves-voice jeeves-alerts
loginctl show-user "$(id -un)" -p Linger
python3 scripts/voicectl.py health
python3 scripts/voicectl.py last-turn
journalctl --user -u jeeves-voice -u jeeves-alerts -n 50 --no-pager
```

Health includes the backend, response mode, wake model, listener state and recent errors.
`last-turn` is an authenticated, RAM-only diagnostic of recognized text/reply; it expires
after five minutes or service restart. No transcripts/keys are written to ordinary logs.
A text response is not independent proof that a physical device moved; verify tool/state
results and the actual device when testing home control.

```bash
python3 scripts/voicectl.py ask 'What is the capital of Japan?' --speak
python3 scripts/voicectl.py audio /path/to/mono-16bit-16000Hz.wav
python3 scripts/alertctl.py list
python3 scripts/alertctl.py dismiss
```

The audio command requires mono, 16-bit PCM WAV at 16kHz. `dismiss` stops ringing alerts
without deleting pending schedules. `cancel all timers`/alarms/reminders affects all
active records of that kind. An individual `/cancel` request can target an ID.

The following smoke test plays all three alert kinds and tests timer restart recovery.
It refuses to run while real alerts are active and cleans up only its own records:

```bash
python3 scripts/smoke-alerts.py
```

The alert API and voice control API bind loopback by default. No LAN listener needs
opening for standalone voice. The optional HA-managed frontend needs explicit LAN
settings described in [its setup guide](HOME_ASSISTANT.md).

## Response mode and voice selection

```bash
python3 scripts/voicectl.py mode quiet
python3 scripts/voicectl.py mode talkative
python3 scripts/voicectl.py mode
```

Quiet mode suppresses successful action confirmations, not answers, failures,
clarification, wake cues or scheduled alert playback. Mode changes persist immediately.
See [backend instructions](BACKENDS.md) for voice equivalents and classification details.

To use another Piper voice, provision its `.onnx` and `.onnx.json` files together and
edit `tts.model` in private `alerts.json` (and `tts.executable` if changing its runtime).
Then restart both services:

```bash
systemctl --user restart jeeves-alerts jeeves-voice
```

## Wake words

Built-ins are Hey Jarvis (default), Okay Nabu, Hey Mycroft, Alexa and Hey Rhasspy. In
private `voice.json`, set `wake.model` to the corresponding ID from
[the backend guide](BACKENDS.md#built-in-wake-words), remove `wake.model_path` if present,
then restart voice. No training is needed for built-ins.

For a custom phrase, use openWakeWord's [simple training notebook](https://colab.research.google.com/drive/1q1oe2zOyZp7UsB3jJiQ1IFn8z5YfjwEb?usp=sharing)
or [training documentation](https://github.com/dscripka/openWakeWord#training-new-models).
Run its data-generation/training/export steps and obtain a `.tflite` model. Renaming an
existing model does not change what it recognizes. Place your model in retained `models/`,
set private `wake.model_path` to its absolute path, and restart:

```bash
systemctl --user restart jeeves-voice
python3 scripts/voicectl.py health --wait 120
```

The standalone detector loads the model directly; no LVA descriptor is needed. Test
at normal distance with intended speakers and background sound. Threshold starts at
0.5; raising it can reduce false activations and lowering it can reduce misses.

## Backup and restore

Retain code, lock files, models, private configuration and SQLite state. Code alone
contains neither keys nor existing schedules. Use a private archive destination;
the example archive contains reminder text and credentials. Stop services briefly for
a consistent database snapshot:

```bash
cd "$HOME/voice-assistant"
systemctl --user stop jeeves-voice jeeves-alerts
umask 077
tar -czf "$HOME/jeeves-private-backup.tgz" .state -C "$HOME" .config/jeeves
systemctl --user start jeeves-alerts jeeves-voice
```

Copy the archive off the machine using your chosen backup method. After recovering
code on a fresh machine, restore before installing:

```bash
cd "$HOME/voice-assistant"
tar -xzf "$HOME/jeeves-private-backup.tgz" .state
tar -xzf "$HOME/jeeves-private-backup.tgz" -C "$HOME" .config/jeeves
bash scripts/install-alerts.sh
export PATH="$HOME/.local/bin:$PATH"
bash scripts/install-voice.sh
```

If the account, checkout path or audio hardware changed, update private state/TTS/STT,
source/sink and credential paths before startup; existing private values are preserved.
Use a distinct session key when cloning configuration onto a second live satellite.
Rotate credential files privately, then restart voice. A changed alert token also needs
voice restart; update the HA secret only if using the optional HA package.

## Cold-boot verification

Boot enablement is not a substitute for a real test on each target. When convenient:

```bash
python3 scripts/alertctl.py 'set a timer for two minutes'
sudo reboot
```

Listen for the timer and try the wake word before logging in. Afterwards inspect:

```bash
systemctl --user is-active jeeves-alerts jeeves-voice
journalctl --user -b -u jeeves-voice -u jeeves-alerts --no-pager
python3 scripts/alertctl.py dismiss
```

See [architecture](ARCHITECTURE.md) for recovery/catch-up limits and
[backend setup](BACKENDS.md) for alternatives. Optional LVA/ESPHome has separate server
configuration in [HOME_ASSISTANT.md](HOME_ASSISTANT.md).
