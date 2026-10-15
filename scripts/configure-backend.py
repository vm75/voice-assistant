#!/usr/bin/python3
"""Store a backend URL and key privately using interactive prompts."""
import argparse
import getpass
from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from jeeves.configuration import private_directory, write_private_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('backend', choices=['hermes', 'homeassistant'])
    args = parser.parse_args()
    hint = 'ending in /v1' if args.backend == 'hermes' else 'without /api'
    url = input(f'{args.backend} base URL ({hint}): ').strip().rstrip('/')
    parsed = urlsplit(url)
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise SystemExit('Enter an HTTP(S) URL without embedded credentials, query or fragment.')
    if args.backend == 'hermes' and not parsed.path.endswith('/v1'):
        raise SystemExit('The Hermes agent API base URL must end in /v1.')
    key = getpass.getpass('API key / access token: ').strip()
    if not key:
        raise SystemExit('A key is required.')
    path = private_directory() / (args.backend + '.json')
    write_private_json(path, {'base_url': url, 'api_key': key})
    print(f'Private {args.backend} credentials saved. Restart voice if it is already running.')


if __name__ == '__main__':
    main()
