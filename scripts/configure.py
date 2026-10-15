#!/usr/bin/python3
"""Create portable private alert settings; preserve existing settings by default."""
import argparse
import ipaddress
import json
from pathlib import Path
import secrets
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from jeeves.configuration import private_directory, render_user_unit, select_device, system_timezone, write_private_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timezone', help='IANA timezone; defaults to the host timezone for new config')
    parser.add_argument('--sink', help='Exact PulseAudio/PipeWire sink name')
    parser.add_argument('--host', help='Alert API bind address; new installations use loopback')
    parser.add_argument('--allow-client', action='append', help='Allowed client IP; repeat as needed (loopback always allowed)')
    args = parser.parse_args()
    private = private_directory()
    path = private / 'alerts.json'
    config = json.loads(path.read_text()) if path.exists() else {
        'token': secrets.token_urlsafe(48), 'host': '127.0.0.1', 'port': 5124,
        'allowed_clients': ['127.0.0.1'], 'timezone': system_timezone(),
        'sink': select_device('sinks', args.sink), 'state_dir': str(ROOT / '.state'),
    }
    if args.timezone:
        ZoneInfo(args.timezone)
        config['timezone'] = args.timezone
    if args.sink:
        config['sink'] = select_device('sinks', args.sink)
    if args.host:
        ipaddress.IPv4Address(args.host)
        config['host'] = args.host
    if args.allow_client is not None:
        config['allowed_clients'] = ['127.0.0.1', *[str(ipaddress.ip_address(client)) for client in args.allow_client if client != '127.0.0.1']]
    write_private_json(path, config)
    # Used only by the optional HA-managed frontend, not by standalone voice.
    secret = private / 'ha-secret.yaml'
    secret.write_text('jeeves_alert_authorization: "Bearer ' + config['token'] + '"\n')
    secret.chmod(0o600)
    render_user_unit(ROOT, 'jeeves-alerts')
    print('Private alert configuration ready; timezone:', config['timezone'])


if __name__ == '__main__':
    main()
