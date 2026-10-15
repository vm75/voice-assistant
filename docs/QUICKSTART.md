# Fresh Ubuntu + USB Jabra setup

Use a normal Ubuntu user with sudo access, not a root shell. Connect the Jabra first.
Ubuntu Server 26.04 LTS x86_64 is the tested target. Installation needs internet and
an existing Hermes **agent API** with an API key; Hermes must already have the desired
Home Assistant/web tools. No desktop or HA voice pipeline is needed.

## 1. Clone and install audio

```bash
sudo apt-get update
sudo apt-get install -y git python3
git clone https://github.com/vm75/voice-assistant.git "$HOME/voice-assistant"
cd "$HOME/voice-assistant"
bash scripts/install-alerts.sh
export PATH="$HOME/.local/bin:$PATH"
```

The installer sets up PipeWire, local neural speech, persistent alerts and boot startup.
New alert configuration uses the host timezone; it does not change the OS timezone.
Check `timedatectl` before setting wall-clock alarms.

## 2. Enter your Hermes connection

```bash
python3 scripts/configure-backend.py hermes
```

Enter your server's HTTP(S) agent API base URL, ending in `/v1`, and its key. The key
prompt is hidden. Credentials are stored with owner-only permissions outside the
repository, in `~/.config/jeeves/hermes.json`. No server address is assumed.

## 3. Start voice

```bash
bash scripts/install-voice.sh
python3 scripts/voicectl.py mode quiet
python3 scripts/voicectl.py health
```

Say **“Hey Jarvis”**, wait for the chime, then ask **“What is the capital of Japan?”**
You should hear an answer. Try **“Set a timer for ten seconds”**: quiet mode skips the
success confirmation, but the timer still sounds when it finishes.

To restore spoken successful-action confirmations:

```bash
python3 scripts/voicectl.py mode talkative
```

Or say “enable talkative mode” / “enable quiet mode”. Answers, clarification and failures
remain audible. Unclassified agent replies also stay audible. The wake chime and actual
scheduled alerts are unaffected. Both mode and backend choices survive restart.

## 4. Check boot startup

When ready to interrupt the machine:

```bash
sudo reboot
```

Test “Hey Jarvis” without first logging in. Services are enabled at boot using user
lingering. If it does not respond, log in and inspect:

```bash
cd "$HOME/voice-assistant"
python3 scripts/voicectl.py health
journalctl --user -b -u jeeves-voice -u jeeves-alerts -n 50 --no-pager
```

Back up private `~/.config/jeeves/` and the repo's `.state/` directory to retain keys
and schedules. For audio overrides, timezone changes, other backends or recovery,
see [the execution guide](EXECUTION.md) and [backend setup](BACKENDS.md).
