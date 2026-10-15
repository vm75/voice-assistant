#!/usr/bin/python3
"""Play all three alert kinds, including recovery across a service restart."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import time
from urllib.request import Request, urlopen

config = json.loads((Path.home() / '.config/jeeves/alerts.json').read_text())
def call(path, data=None):
    request = Request(f'http://127.0.0.1:{config["port"]}{path}',
        data=None if data is None else json.dumps(data).encode(),
        headers={'Authorization': 'Bearer ' + config['token'], 'Content-Type': 'application/json'})
    with urlopen(request, timeout=5) as response:
        return json.load(response)

def ready():
    for _ in range(40):
        try:
            if call('/health')['scheduler_alive']:
                return
        except OSError:
            pass
        time.sleep(.25)
    raise RuntimeError('Service not ready')

ready()
if call('/alerts')['alerts']:
    raise SystemExit('Active alerts exist; smoke test will not interrupt them.')
for kind in ('timer', 'alarm', 'reminder'):
    old_success = call('/health')['audio_last_success'] or 0
    if kind == 'alarm':
        when = (datetime.now(timezone.utc) + timedelta(seconds=3)).isoformat()
        command = f'set an alarm for {when}'
    elif kind == 'timer':
        command = 'set a timer for three seconds'
    else:
        command = 'remind me to test local reminder audio in three seconds'
    result = call('/command', {'command': command})
    key = result['alert']['id']
    if kind == 'timer':
        subprocess.run(['systemctl', '--user', 'restart', 'jeeves-alerts'], check=True)
        ready()
    try:
        for _ in range(120):
            health = call('/health')
            if (health['audio_last_success'] or 0) > old_success:
                assert not health['audio_last_error'], health
                print(json.dumps({'kind': kind, 'id': key, 'audio_success': health['audio_last_success'], 'restart_recovery': kind == 'timer'}), flush=True)
                break
            if health['audio_last_error']:
                raise RuntimeError(health['audio_last_error'])
            time.sleep(.25)
        else:
            raise RuntimeError(f'{kind} playback did not complete')
    finally:
        call('/cancel', {'id': key})
