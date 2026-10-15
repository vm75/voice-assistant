#!/usr/bin/python3
"""Generate host-independent voice defaults; endpoints/keys stay in private files."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from jeeves.backends import create_backend
from jeeves.configuration import device_identity, private_directory, render_user_unit, select_device, write_private_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', help='Exact PulseAudio/PipeWire microphone source name')
    args = parser.parse_args()
    private = private_directory()
    path = private / 'voice.json'
    if path.exists():
        config = json.loads(path.read_text())
        if args.source:
            config['source'] = select_device('sources', args.source)
    else:
        import secrets
        config = {
            'backend': 'hermes', 'response_mode': 'talkative', 'port': 5125,
            'control_token': secrets.token_urlsafe(48),
            'source': select_device('sources', args.source), 'alerts_config': str(private / 'alerts.json'),
            'wake': {'model': 'hey_jarvis', 'threshold': 0.5},
            'recording': {'vad_mode': 2, 'silence_seconds': 1.0, 'max_seconds': 20, 'wait_seconds': 5},
            'stt': {'model_path': str(ROOT / 'models/stt/base.en'), 'language': 'en', 'cpu_threads': 2},
            'backends': {
                'hermes': {'type': 'openai', 'credentials_file': str(private / 'hermes.json'),
                    'model': 'hermes-agent', 'timeout': 120,
                    'session_header': 'X-Hermes-Session-Id', 'session_key_header': 'X-Hermes-Session-Key',
                    'session_key': 'agent:jeeves:voice:' + device_identity()},
                'homeassistant': {'type': 'homeassistant', 'credentials_file': str(private / 'homeassistant.json'),
                    'agent_id': 'home_assistant', 'language': 'en', 'timeout': 30},
            },
        }
    selected = config.get('backend', 'hermes')
    create_backend(selected, config['backends'][selected])
    write_private_json(path, config)
    render_user_unit(ROOT, 'jeeves-voice')
    print('Private voice configuration ready; backend:', selected)


if __name__ == '__main__':
    main()
