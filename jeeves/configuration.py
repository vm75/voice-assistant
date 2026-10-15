"""Portable setup helpers; deployment data lives in the user's private directory."""
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
from zoneinfo import ZoneInfo


def private_directory():
    directory = Path.home() / '.config/jeeves'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    return directory


def write_private_json(path, value):
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(value, output, indent=2)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def system_timezone():
    """Read the host timezone without changing it; UTC is the portable fallback."""
    candidates = []
    localtime = str(Path('/etc/localtime').resolve())
    if '/zoneinfo/' in localtime:
        candidates.append(localtime.split('/zoneinfo/', 1)[1])
    timezone_file = Path('/etc/timezone')
    if timezone_file.is_file():
        candidates.append(timezone_file.read_text().strip())
    for candidate in candidates:
        try:
            ZoneInfo(candidate)
            return candidate
        except (ValueError, KeyError):
            continue
    return 'UTC'


def select_device(kind, explicit=None):
    devices = json.loads(subprocess.check_output(
        ['pactl', '--format=json', 'list', kind], timeout=5))
    if explicit:
        matches = [device['name'] for device in devices if device['name'] == explicit]
    else:
        matches = [device['name'] for device in devices if 'jabra' in device['name'].lower()
                   and not device['name'].endswith('.monitor')]
    if len(matches) != 1:
        raise ValueError(f'Expected one audio device. Inspect pactl list short {kind} and select its name explicitly.')
    return matches[0]


def device_identity():
    return re.sub('[^a-z0-9-]', '-', socket.gethostname().lower()).strip('-') or 'voice-assistant'


def render_user_unit(root, name):
    units = Path.home() / '.config/systemd/user'
    units.mkdir(parents=True, exist_ok=True)
    template = (root / 'systemd' / (name + '.service.in')).read_text()
    (units / (name + '.service')).write_text(template.replace('@ROOT@', str(root)))
