import getpass
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from jeeves.configuration import device_identity, select_device, write_private_json

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.home_patch = patch.object(Path, 'home', return_value=self.home)
        self.home_patch.start()
        self.output_patch = patch('sys.stdout', new_callable=io.StringIO)
        self.output = self.output_patch.start()

    def tearDown(self):
        self.output_patch.stop()
        self.home_patch.stop()
        self.tmp.cleanup()

    def test_new_alert_settings_use_host_timezone_and_loopback_only(self):
        script = load_script('configure')
        with patch.object(sys, 'argv', ['configure.py']), patch.object(script, 'system_timezone', return_value='Europe/Paris'), patch.object(script, 'select_device', return_value='jabra-sink'):
            script.main()
        private = self.home / '.config/jeeves'
        config = json.loads((private / 'alerts.json').read_text())
        self.assertEqual(config['timezone'], 'Europe/Paris')
        self.assertEqual(config['host'], '127.0.0.1')
        self.assertEqual(config['allowed_clients'], ['127.0.0.1'])
        self.assertEqual(config['sink'], 'jabra-sink')
        self.assertEqual((private / 'alerts.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(private.stat().st_mode & 0o777, 0o700)
        self.assertNotIn(config['token'], self.output.getvalue())

    def test_reconfiguration_preserves_settings_and_allows_explicit_timezone(self):
        script = load_script('configure')
        with patch.object(sys, 'argv', ['configure.py']), patch.object(script, 'select_device', return_value='jabra-sink'):
            script.main()
        path = self.home / '.config/jeeves/alerts.json'
        previous = json.loads(path.read_text())
        with patch.object(sys, 'argv', ['configure.py', '--timezone', 'Asia/Tokyo']), patch.object(script, 'select_device', side_effect=AssertionError('Existing device must be preserved')):
            script.main()
        updated = json.loads(path.read_text())
        self.assertEqual(updated['timezone'], 'Asia/Tokyo')
        for key in ['token', 'host', 'allowed_clients', 'sink', 'state_dir']:
            self.assertEqual(updated[key], previous[key])

    def test_fresh_voice_uses_credentials_url_and_hostname_identity(self):
        private = self.home / '.config/jeeves'
        private.mkdir(parents=True)
        write_private_json(private / 'hermes.json', {'base_url': 'https://agent.example/v1', 'api_key': 'test-key'})
        script = load_script('configure-voice')
        with patch.object(sys, 'argv', ['configure-voice.py']), patch.object(script, 'select_device', return_value='jabra-source'), patch.object(script, 'device_identity', return_value='speaker-a'):
            script.main()
        config = json.loads((private / 'voice.json').read_text())
        self.assertEqual(config['backend'], 'hermes')
        self.assertEqual(config['backends']['hermes']['session_key'], 'agent:jeeves:voice:speaker-a')
        self.assertNotIn('base_url', config['backends']['hermes'])
        self.assertNotIn('base_url', config['backends']['homeassistant'])
        unit = (self.home / '.config/systemd/user/jeeves-voice.service').read_text()
        self.assertNotIn('@ROOT@', unit)
        self.assertIn(str(ROOT), unit)

    def test_credentials_prompt_never_prints_key_and_creates_private_file(self):
        script = load_script('configure-backend')
        with patch.object(sys, 'argv', ['configure-backend.py', 'hermes']), patch('builtins.input', return_value='https://agent.example/v1'), patch.object(getpass, 'getpass', return_value='private-fixture-key'):
            script.main()
        path = self.home / '.config/jeeves/hermes.json'
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(path.read_text())['base_url'], 'https://agent.example/v1')
        self.assertNotIn('private-fixture-key', self.output.getvalue())

    def test_device_selection_requires_explicit_choice_when_ambiguous(self):
        data = json.dumps([{'name': 'jabra-a'}, {'name': 'jabra-b'}]).encode()
        with patch('jeeves.configuration.subprocess.check_output', return_value=data):
            with self.assertRaises(ValueError):
                select_device('sinks')
            self.assertEqual(select_device('sinks', 'jabra-b'), 'jabra-b')

    def test_hostname_identity_is_machine_derived(self):
        with patch('jeeves.configuration.socket.gethostname', return_value='Speaker_Office'):
            self.assertEqual(device_identity(), 'speaker-office')


if __name__ == '__main__':
    unittest.main()
