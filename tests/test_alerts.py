import json
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from jeeves.alerts import Application, Audio, Store, alarm_time, duration, handler, parse_command

ZONE = ZoneInfo('America/New_York')
NOW = datetime(2026, 10, 15, 12, tzinfo=ZONE).timestamp()


class FakeAudio:
    last_error = None
    last_success = None
    def stop_if(self, predicate):
        pass


class ParsingTests(unittest.TestCase):
    def test_durations(self):
        for text, seconds in [('five minutes', 300), ('1 hour and thirty five minutes', 5700), ('a minute', 60), ('twenty-one seconds', 21)]:
            self.assertEqual(duration(text), seconds)
        for text in ['zero seconds', '-2 seconds', 'later', 'five minutes garbage', '999 days', 'a half hour']:
            with self.assertRaises(ValueError, msg=text):
                duration(text)

    def test_clock_next_day_and_timezone(self):
        due = alarm_time('7 AM', ZONE, NOW)
        self.assertEqual(datetime.fromtimestamp(due, ZONE).isoformat(), '2026-10-16T07:00:00-04:00')
        self.assertEqual(alarm_time('seven pm', ZONE, NOW), alarm_time('19:00', ZONE, NOW))
        self.assertEqual(alarm_time('2026-10-15T20:00:00-04:00', ZONE, NOW), NOW + 8 * 3600)
        for text in ['today 7 AM', '25:00', '7:99', '0 PM', '2026-10-15T20:00:00']:
            with self.assertRaises(ValueError, msg=text):
                alarm_time(text, ZONE, NOW)

    def test_dst_gap_and_fold(self):
        for now, text in [(datetime(2027, 3, 13, 12, tzinfo=ZONE).timestamp(), 'tomorrow 2:30 AM'), (datetime(2026, 10, 31, 12, tzinfo=ZONE).timestamp(), 'tomorrow 1:30 AM')]:
            with self.assertRaises(ValueError):
                alarm_time(text, ZONE, now)

    def test_reminder_text_and_missing_time(self):
        spec = parse_command('Remind me to check in at work in two hours.', ZONE, NOW)
        self.assertEqual(spec, dict(kind='reminder', due=NOW + 7200, text='check in at work'))
        with self.assertRaises(ValueError):
            parse_command('remind me to buy milk', ZONE, NOW)

    def test_controls(self):
        self.assertEqual(parse_command('cancel all alarms', ZONE, NOW), dict(action='cancel', kind='alarm'))
        self.assertEqual(parse_command('stop the alarm', ZONE, NOW), dict(action='dismiss'))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'alerts.db'
        self.store = Store(self.path)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_multiple_reminders_and_dedup(self):
        a = self.store.create('reminder', 'one', NOW, 'request1')
        b = self.store.create('reminder', 'two', NOW, 'request2')
        self.assertNotEqual(a['id'], b['id'])
        self.assertEqual(self.store.create('reminder', 'one', NOW, 'request1')['id'], a['id'])
        self.assertEqual(len(self.store.active()), 2)

    def test_recovery_repeat_and_dismiss(self):
        a = self.store.create('timer', 'test', NOW)
        self.store.db.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.tick(NOW + 10)['id'], a['id'])
        self.assertIsNone(self.store.tick(NOW + 11))
        self.assertEqual(self.store.tick(NOW + 25)['id'], a['id'])
        self.store.stop('dismissed')
        self.assertIsNone(self.store.tick(NOW + 60))

    def test_ring_limit_survives_restart(self):
        self.store.create('alarm', 'test', NOW)
        self.store.tick(NOW)
        self.store.db.close()
        self.store = Store(self.path)
        self.assertIsNone(self.store.tick(NOW + 901))
        self.assertEqual(self.store.active(), [])

    def test_missed_and_cancelled(self):
        self.store.create('timer', 'too old', NOW - 86401)
        self.store.create('alarm', 'cancel', NOW)
        self.store.stop('cancelled', 'alarm')
        self.assertIsNone(self.store.tick(NOW))
        self.assertEqual(self.store.active(), [])

    def test_pending_not_dismissed(self):
        self.store.create('timer', 'future', NOW + 600)
        self.assertEqual(self.store.stop('dismissed'), 0)
        self.assertEqual(len(self.store.active()), 1)

    def test_fairness_simultaneous_due(self):
        keys = {self.store.create('reminder', str(i), NOW)['id'] for i in range(3)}
        sounded = {self.store.tick(NOW + i)['id'] for i in range(3)}
        self.assertEqual(sounded, keys)


class APITests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.app = Application(self.store, FakeAudio(), 'America/New_York')
        self.app.worker = threading.current_thread()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler(self.app, 'x' * 48))
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.store.db.close()

    def call(self, path, data=None, token='x' * 48):
        request = Request(f'http://127.0.0.1:{self.server.server_port}{path}',
            data=json.dumps(data).encode() if data is not None else None,
            headers={'Authorization': 'Bearer ' + token})
        return json.load(urlopen(request, timeout=2))

    def test_auth_and_allowlist(self):
        with self.assertRaises(HTTPError) as caught:
            self.call('/health', token='wrong')
        self.assertEqual(caught.exception.code, 401)
        caught.exception.close()
        self.app.allowed_clients = ['192.0.2.1']
        with self.assertRaises(HTTPError) as caught:
            self.call('/health')
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()

    def test_save_list_cancel_and_validation(self):
        saved = self.call('/command', {'command': 'set a timer for five minutes', 'request_id': 'one'})
        self.assertIn('saved', saved['speech'])
        self.assertEqual(len(self.call('/alerts')['alerts']), 1)
        with self.assertRaises(HTTPError) as caught:
            self.call('/command', {'command': 'set an alarm for banana'})
        self.assertEqual(caught.exception.code, 400)
        caught.exception.close()
        self.assertEqual(self.call('/cancel', {'id': saved['alert']['id']})['count'], 1)
        self.assertEqual(self.call('/alerts')['alerts'], [])


class AudioTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.store.create('timer', 'test', NOW)
        self.alert = self.store.tick(NOW)
        self.audio = Audio('jabra-test')

    def tearDown(self):
        self.store.db.close()

    def test_missing_sink_does_not_launch_player_and_remains_retryable(self):
        with patch('jeeves.alerts.subprocess.check_output', return_value=b'[]'), patch('jeeves.alerts.subprocess.Popen') as player:
            with self.assertLogs('jeeves', level='ERROR'):
                self.audio.play(self.alert, self.store)
            player.assert_not_called()
        self.assertIn('refusing audio fallback', self.audio.last_error)
        self.assertEqual(self.store.tick(NOW + 15)['id'], self.alert['id'])

    def test_piper_synthesizes_text_without_shell_interpolation(self):
        player = Mock(returncode=0)
        player.communicate.return_value = (b'', b'')
        with patch('jeeves.alerts.subprocess.check_output', return_value=b'[{"name":"jabra-test"}]'), patch('jeeves.alerts.subprocess.run') as synth, patch('jeeves.alerts.subprocess.Popen', return_value=player):
            self.audio.play(self.alert, self.store)
        self.assertTrue(synth.call_args.args[0][0].endswith('/piper'))
        self.assertIn('en_US-lessac-high.onnx', synth.call_args.args[0][2])
        self.assertEqual(synth.call_args.kwargs['input'], 'timer. test\n')
        self.assertNotIn('shell', synth.call_args.kwargs)
        self.assertIsNone(self.audio.last_error)

    def test_speech_failure_still_sounds_chime_on_selected_sink(self):
        player = Mock(returncode=0)
        player.communicate.return_value = (b'', b'')
        with patch('jeeves.alerts.subprocess.check_output', return_value=b'[{"name":"jabra-test"}]'), patch('jeeves.alerts.subprocess.run', side_effect=FileNotFoundError('piper missing')), patch('jeeves.alerts.subprocess.Popen', return_value=player) as launch:
            with self.assertLogs('jeeves', level='WARNING'):
                self.audio.play(self.alert, self.store)
        args = launch.call_args.args[0]
        self.assertIn('--audio-device=pulse/jabra-test', args)
        self.assertFalse(any(arg.startswith('--ao=') for arg in args))
        self.assertIn('--no-config', args)
        self.assertTrue(args[-1].endswith('chime.wav'))
        self.assertIn('Speech synthesis failed', self.audio.last_error)
        self.assertIsNotNone(self.audio.last_success)


if __name__ == '__main__':
    unittest.main()
