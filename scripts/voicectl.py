#!/usr/bin/python3
"""Private local control: ask, audio, backend selection and health."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from jeeves.backends import create_backend
from jeeves.voice_audio import read_wav


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path.home() / '.config/jeeves/voice.json')
    sub = parser.add_subparsers(dest='action', required=True)
    health = sub.add_parser('health')
    health.add_argument('--wait', type=int, default=0)
    sub.add_parser('last-turn')
    mode = sub.add_parser('mode')
    mode.add_argument('name', nargs='?', choices=['quiet', 'talkative'])
    ask = sub.add_parser('ask')
    ask.add_argument('text')
    ask.add_argument('--speak', action='store_true')
    audio = sub.add_parser('audio')
    audio.add_argument('wav', type=Path)
    backend = sub.add_parser('backend')
    backend.add_argument('name', nargs='?')
    backend.add_argument('--no-restart', action='store_true')
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    if args.action == 'backend':
        if not args.name:
            print('Selected:', config.get('backend', 'hermes'))
            print('Configured:', ', '.join(config['backends']))
            return
        if args.name not in config['backends']:
            raise SystemExit('Unknown backend; add its adapter configuration first.')
        create_backend(args.name, config['backends'][args.name])  # Reject missing credentials before writing.
        config['backend'] = args.name
        fd, temporary = tempfile.mkstemp(dir=args.config.parent)
        with os.fdopen(fd, 'w') as output:
            json.dump(config, output, indent=2)
        os.replace(temporary, args.config)
        if not args.no_restart:
            subprocess.run(['systemctl', '--user', 'restart', 'jeeves-voice'], check=True)
        print('Selected backend:', args.name)
        return
    endpoint = {'health': '/health', 'last-turn': '/last-turn', 'audio': '/audio', 'ask': '/ask', 'mode': '/mode'}[args.action]
    data = None
    if args.action == 'mode' and args.name:
        data = json.dumps({'mode': args.name}).encode()
    elif args.action == 'ask':
        data = json.dumps({'text': args.text, 'speak': args.speak}).encode()
    elif args.action == 'audio':
        data = read_wav(args.wav)
    deadline = time.monotonic() + (args.wait if args.action == 'health' else 0)
    while True:
        request = urllib.request.Request(f'http://127.0.0.1:{config.get("port",5125)}{endpoint}',
            data=data, headers={'Authorization': 'Bearer ' + config['control_token'],
                               'Content-Type': 'application/octet-stream' if args.action == 'audio' else 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                result = json.load(response)
            if args.action == 'health' and args.wait and (not result.get('listener_alive') or result.get('state') != 'listening'):
                raise OSError('Voice listener not ready')
            print(json.dumps(result, indent=2))
            wants_speech = args.action == 'audio' or (args.action == 'ask' and args.speak)
            if args.action in {'ask', 'audio'} and (not result.get('success', False) or (wants_speech and not result.get('spoken') and not result.get('speech_suppressed'))):
                raise SystemExit(1)
            return
        except (OSError, ValueError):
            if args.action == 'health' and time.monotonic() < deadline:
                time.sleep(.5)
                continue
            raise SystemExit('Voice service request failed; inspect journalctl --user -u jeeves-voice.') from None


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError):
        raise SystemExit('Voice configuration or credentials are invalid; see docs/BACKENDS.md.') from None
