#!/usr/bin/python3
"""Check public documentation links/examples and accidental deployment assumptions."""
import ipaddress
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent


def check():
    errors = []
    public = [p for directory in ['docs', 'jeeves', 'scripts', 'systemd', 'tests', 'ha', '.github']
              for p in (ROOT / directory).rglob('*')
              if p.is_file() and p.suffix in {'.md', '.py', '.sh', '.in', '.yml', '.yaml'}]
    public += list(ROOT.glob('*.md'))
    documents = [p for p in public if p.suffix == '.md']
    for path in public:
        text = path.read_text()
        relative = path.relative_to(ROOT)
        if path.suffix == '.md' and path != ROOT / 'README.md' and ROOT / 'docs' not in path.parents:
            errors.append(f'{relative}: move project documentation under docs/')
        for literal in re.findall(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', text):
            try:
                address = ipaddress.ip_address(literal)
            except ValueError:
                continue
            if any(address in network for network in (
                    ipaddress.ip_network((0x0A000000, 8)), ipaddress.ip_network((0xAC100000, 12)),
                    ipaddress.ip_network((0xC0A80000, 16)))):
                errors.append(f'{relative}: contains a deployment-specific private network address')
        if re.search(r'/home/[a-zA-Z0-9_.-]+', text):
            errors.append(f'{relative}: contains a hardcoded user home directory')
    for path in documents:
        text = path.read_text()
        for target in re.findall(r'\]\(([^)]+)\)', text):
            if target.startswith(('https://', 'http://', 'mailto:')):
                continue
            local, _, anchor = unquote(target).partition('#')
            destination = (path.parent / local).resolve() if local else path
            if not destination.exists():
                errors.append(f'{path.relative_to(ROOT)}: missing link {target}')
            elif anchor and destination.suffix == '.md':
                headings = re.findall(r'^#{1,6}\s+(.+)$', destination.read_text(), re.M)
                anchors = {re.sub(r'[^\w -]', '', heading.lower()).replace(' ', '-') for heading in headings}
                if anchor not in anchors:
                    errors.append(f'{path.relative_to(ROOT)}: missing anchor {target}')
        for language, code in re.findall(r'```(\w+)\n(.*?)\n```', text, re.S):
            if language == 'bash':
                result = subprocess.run(['bash', '-n'], input=code, text=True, capture_output=True)
                if result.returncode:
                    errors.append(f'{path.relative_to(ROOT)}: invalid shell example: {result.stderr.strip()}')
            elif language == 'json':
                try:
                    json.loads(code)
                except ValueError:
                    errors.append(f'{path.relative_to(ROOT)}: invalid JSON example')
    if errors:
        raise SystemExit('\n'.join(errors))
    print(f'Checked {len(documents)} Markdown files: local links, shell/JSON examples and deployment portability.')


if __name__ == '__main__':
    check()
