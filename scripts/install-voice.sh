#!/usr/bin/env bash
# Default standalone voice path: local wake/STT/TTS -> selected agent API.
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
cd "$(dirname "$0")/.."
mkdir -p logs vendor/voice models/stt
exec > >(tee -a logs/install-voice.log) 2>&1
set -x
if [ ! -f "$HOME/.config/jeeves/alerts.json" ]; then
    bash scripts/install-alerts.sh
fi
if ! command -v uv >/dev/null; then
    curl -fsSL https://astral.sh/uv/0.13.1/install.sh -o /tmp/jeeves-install-uv.sh
    sh /tmp/jeeves-install-uv.sh
    export PATH="$HOME/.local/bin:$PATH"
fi
uv python install 3.13.16
if [ ! -x vendor/voice/.venv/bin/python ]; then
    uv venv --python 3.13.16 vendor/voice/.venv
fi
uv pip sync --python vendor/voice/.venv/bin/python locks/voice-requirements.txt
vendor/voice/.venv/bin/python - <<'PY'
from huggingface_hub import snapshot_download
snapshot_download('Systran/faster-whisper-base.en',
    revision='3d3d5dee26484f91867d81cb899cfcf72b96be6c', local_dir='models/stt/base.en',
    allow_patterns=['model.bin','config.json','tokenizer.json','vocabulary.*','preprocessor_config.json'])
PY
/usr/bin/python3 scripts/configure-voice.py
# The LVA ESPHome path remains optional, but two always-listening frontends must
# not process the same utterance. Disable it when switching to standalone voice.
if systemctl --user cat jeeves-satellite.service >/dev/null 2>&1; then
    systemctl --user disable --now jeeves-satellite.service
fi
systemctl --user daemon-reload
systemctl --user enable jeeves-voice.service
systemctl --user restart jeeves-voice.service
/usr/bin/python3 scripts/voicectl.py health --wait 120
