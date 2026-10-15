#!/usr/bin/python3
"""CLI for local testing and stop control, without tokens in shell history."""
import argparse
import json
from pathlib import Path
import urllib.error
import urllib.request

parser = argparse.ArgumentParser()
parser.add_argument('command', nargs='*', help='Spoken command, or health/list/dismiss')
args = parser.parse_args()
config = json.loads((Path.home() / '.config/jeeves/alerts.json').read_text())
command = ' '.join(args.command)
path, data = '/command', {'command': command}
if command in {'health', 'list'}:
    path, data = ('/health' if command == 'health' else '/alerts'), None
elif command == 'dismiss':
    path, data = '/dismiss', {}
request = urllib.request.Request(f'http://127.0.0.1:{config["port"]}{path}',
    data=json.dumps(data).encode() if data is not None else None,
    headers={'Authorization': 'Bearer ' + config['token'], 'Content-Type': 'application/json'})
try:
    with urllib.request.urlopen(request, timeout=10) as response:
        print(json.dumps(json.load(response), indent=2))
except urllib.error.HTTPError as exc:
    print(exc.read().decode())
    raise SystemExit(1)
