#!/usr/bin/env bash
# Optional phase 1. Requires uv; see docs/EXECUTION.md. No remote HA changes.
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
cd "$(dirname "$0")/.."
mkdir -p logs locks vendor
exec > >(tee -a logs/install-satellite.log) 2>&1
set -x
sudo apt-get install -y git build-essential libmpv-dev libasound2-dev libasound2-plugins avahi-utils ca-certificates
command -v uv
uv python install 3.13
if [ ! -d vendor/linux-voice-assistant/.git ]; then
    git clone https://github.com/OHF-Voice/linux-voice-assistant.git vendor/linux-voice-assistant
fi
# Pinned upstream revision for reproducible installation.
git -C vendor/linux-voice-assistant checkout --detach b49fd8dc6534111c12d08b18acbfd1135dc1b082
uv venv --python 3.13 vendor/linux-voice-assistant/.venv
if [ ! -f locks/lva-requirements.txt ]; then
    uv pip compile --python vendor/linux-voice-assistant/.venv/bin/python vendor/linux-voice-assistant/pyproject.toml -o locks/lva-requirements.txt
fi
uv pip sync --python vendor/linux-voice-assistant/.venv/bin/python locks/lva-requirements.txt
uv pip install --python vendor/linux-voice-assistant/.venv/bin/python --no-deps -e vendor/linux-voice-assistant
/usr/bin/python3 - <<'PY'
import json
from pathlib import Path
import subprocess
import socket
root = Path.cwd()
config_dir = Path.home() / '.config/jeeves'
config = json.loads((config_dir / 'alerts.json').read_text())
sources = json.loads(subprocess.check_output(['pactl', '--format=json', 'list', 'sources']))
matches = [s['name'] for s in sources if 'jabra' in s['name'].lower() and not s['name'].endswith('.monitor')]
if len(matches) != 1:
    raise SystemExit('Expected exactly one Jabra microphone. Check pactl list short sources.')
env = config_dir / 'satellite.env'
existing = env.read_text() if env.exists() else ''
defaults = {'JEEVES_SOURCE': matches[0], 'JEEVES_SINK': config['sink'],
            'JEEVES_NAME': 'jeeves-' + socket.gethostname(), 'JEEVES_BIND_ADDRESS': '0.0.0.0'}
known = {line.split('=', 1)[0] for line in existing.splitlines() if '=' in line}
additions = ''.join(f'{key}={value}\n' for key, value in defaults.items() if key not in known)
if additions:
    env.write_text(existing.rstrip('\n') + ('\n' if existing else '') + additions)
env.chmod(0o600)
unit = (root / 'systemd/jeeves-satellite.service.in').read_text().replace('@ROOT@', str(root))
(Path.home() / '.config/systemd/user/jeeves-satellite.service').write_text(unit)
PY
vendor/linux-voice-assistant/.venv/bin/linux-voice-assistant --list-input-devices
vendor/linux-voice-assistant/.venv/bin/linux-voice-assistant --list-output-devices
if systemctl --user cat jeeves-voice.service >/dev/null 2>&1; then
    systemctl --user disable --now jeeves-voice.service
fi
systemctl --user daemon-reload
systemctl --user enable --now jeeves-satellite.service
systemctl --user is-active jeeves-satellite.service
