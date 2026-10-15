#!/usr/bin/env bash
# Install an offline neural voice; preserve any explicitly configured custom voice.
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
cd "$(dirname "$0")/.."
mkdir -p logs vendor/piper-tts models/tts
exec > >(tee -a logs/install-tts.log) 2>&1
set -x
if ! command -v uv >/dev/null; then
    curl -fsSL https://astral.sh/uv/0.13.1/install.sh -o /tmp/jeeves-install-uv.sh
    sh /tmp/jeeves-install-uv.sh
    export PATH="$HOME/.local/bin:$PATH"
fi
uv python install 3.13.16
if [ ! -x vendor/piper-tts/.venv/bin/python ]; then
    uv venv --python 3.13.16 vendor/piper-tts/.venv
fi
uv pip sync --python vendor/piper-tts/.venv/bin/python locks/piper-requirements.txt
voice_url=https://huggingface.co/rhasspy/piper-voices/resolve/c10ece1aade47bb51c153c893d14e5bf8e5b7117/en/en_US/lessac/high
for voice_file in en_US-lessac-high.onnx en_US-lessac-high.onnx.json; do
    if ! (cd models/tts && sha256sum -c SHA256SUMS --ignore-missing 2>/dev/null | grep -Fx "$voice_file: OK" >/dev/null); then
        curl -fL --retry 3 "$voice_url/$voice_file" -o "models/tts/$voice_file.partial"
        mv "models/tts/$voice_file.partial" "models/tts/$voice_file"
    fi
done
(cd models/tts && sha256sum -c SHA256SUMS)
/usr/bin/python3 - <<'PY'
import json
import os
from pathlib import Path
import tempfile
root = Path.cwd()
config_path = Path.home() / '.config/jeeves/alerts.json'
config = json.loads(config_path.read_text())
config.setdefault('tts', {
    'executable': str(root / 'vendor/piper-tts/.venv/bin/piper'),
    'model': str(root / 'models/tts/en_US-lessac-high.onnx'),
})
fd, temporary = tempfile.mkstemp(dir=config_path.parent)
with os.fdopen(fd, 'w') as output:
    json.dump(config, output, indent=2)
os.replace(temporary, config_path)
PY
