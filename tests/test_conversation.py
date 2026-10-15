from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock, patch

from jeeves.backends import BackendError, Reply, create_backend
from jeeves.conversation import AlertClient, Router, is_alert_command
from jeeves.voice import Coordinator, VoiceRuntime, handler


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.secret = Path(self.tmp.name) / 'credentials.json'
        self.secret.write_text(json.dumps({'api_key': 'private-test-token'}))
        self.requests = []
        self.status = 200
        self.result = {'choices': [{'finish_reason': 'stop', 'message': {'content': 'It is sunny.'}}]}
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                owner.requests.append((self.path, dict(self.headers), json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                if owner.status == 0:
                    self.close_connection = True
                    return
                self.send_response(owner.status)
                if owner.status == 302:
                    self.send_header('Location', 'http://127.0.0.1:1/stolen')
                self.end_headers()
                self.wfile.write(json.dumps(owner.result).encode())
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.spec = {'type': 'openai', 'credentials_file': str(self.secret),
                     'base_url': f'http://127.0.0.1:{self.server.server_port}/v1',
                     'model': 'replacement-agent', 'session_header': 'X-Hermes-Session-Id'}

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.tmp.cleanup()

    def test_generic_agent_auth_history_and_session_reset(self):
        agent = create_backend('other-claw', self.spec)
        self.assertEqual(agent.respond('weather?'), Reply('It is sunny.', 'other-claw'))
        agent.respond('tomorrow?')
        path, headers, body = self.requests[-1]
        self.assertEqual(path, '/v1/chat/completions')
        self.assertEqual(headers['Authorization'], 'Bearer private-test-token')
        self.assertEqual(body['model'], 'replacement-agent')
        self.assertEqual([m['content'] for m in body['messages'][1:]], ['weather?', 'It is sunny.', 'tomorrow?'])
        self.assertEqual(headers['X-Hermes-Session-Id'], self.requests[0][1]['X-Hermes-Session-Id'])
        old = agent.session_id
        agent.reset()
        agent.respond('new question')
        self.assertNotEqual(old, agent.session_id)
        self.assertEqual(len(self.requests[-1][2]['messages']), 2)

    def test_error_and_redirect_are_not_retried(self):
        for status in (401, 500, 302, 0):
            self.status = status
            self.requests.clear()
            with self.assertRaises(BackendError) as caught:
                create_backend('hermes', self.spec).respond('turn on a light')
            self.assertNotIn('private-test-token', str(caught.exception))
            self.assertEqual(len(self.requests), 1)

    def test_failed_response_does_not_enter_history(self):
        self.result = {'choices': [{'finish_reason': 'error', 'message': {'content': 'unfinished'}}]}
        backend = create_backend('hermes', self.spec)
        with self.assertRaises(BackendError):
            backend.respond('hello')
        self.assertEqual(backend.history, [])

    def test_unexecuted_tools_are_rejected(self):
        self.result = {'choices': [{'finish_reason': 'stop', 'message': {'content': 'Doing it', 'tool_calls': [{}]}}]}
        with self.assertRaises(BackendError):
            create_backend('hermes', self.spec).respond('do something')

    def test_homeassistant_alternative_and_error_response(self):
        spec = {**self.spec, 'type': 'homeassistant', 'base_url': self.spec['base_url'][:-3]}
        backend = create_backend('homeassistant', spec)
        self.result = {'conversation_id': 'ha-conversation', 'response': {'response_type': 'action_done', 'speech': {'plain': {'speech': 'Light is on.'}}}}
        self.assertEqual(backend.respond('turn on light'), Reply('Light is on.', 'homeassistant', True, 'action_success'))
        backend.respond('turn it off')
        self.assertEqual(self.requests[-1][0], '/api/conversation/process')
        self.assertEqual(self.requests[-1][2]['conversation_id'], 'ha-conversation')
        self.assertEqual(self.requests[-1][2]['agent_id'], 'home_assistant')
        self.result['response']['response_type'] = 'error'
        self.assertFalse(backend.respond('unknown device').success)

    def test_agent_response_categories_are_parsed_without_speaking_metadata(self):
        for kind in ('action_success', 'answer', 'clarification', 'error'):
            self.result = {'choices': [{'finish_reason': 'stop', 'message': {
                'content': json.dumps({'text': 'Spoken response', 'response_type': kind})}}]}
            reply = create_backend('hermes', self.spec).respond('request')
            self.assertEqual(reply.text, 'Spoken response')
            self.assertEqual(reply.response_type, kind)
            self.assertEqual(reply.success, kind != 'error')

    def test_unrecognized_agent_metadata_is_not_classified_as_success(self):
        self.result = {'choices': [{'finish_reason': 'stop', 'message': {
            'content': json.dumps({'text': 'Not sure', 'response_type': 'pending'})}}]}
        reply = create_backend('hermes', self.spec).respond('request')
        self.assertEqual(reply.text, 'Not sure')
        self.assertEqual(reply.response_type, 'unknown')

    def test_homeassistant_partial_failure_and_clarification_remain_audible(self):
        spec = {**self.spec, 'type': 'homeassistant', 'base_url': self.spec['base_url'][:-3]}
        backend = create_backend('homeassistant', spec)
        self.result = {'response': {'response_type': 'action_done',
            'data': {'failed': [{'id': 'cover.test'}]}, 'speech': {'plain': {'speech': 'Failed to open.'}}}}
        reply = backend.respond('open cover')
        self.assertFalse(reply.success)
        self.assertEqual(reply.response_type, 'error')
        self.result = {'continue_conversation': True, 'response': {'response_type': 'action_done',
            'speech': {'plain': {'speech': 'Which room?'}}}}
        self.assertEqual(backend.respond('open window').response_type, 'clarification')

    def test_missing_credentials_fail_without_secrets(self):
        self.secret.unlink()
        with self.assertRaises(ValueError):
            create_backend('homeassistant', self.spec)

    def test_idle_timeout_and_bounded_history(self):
        agent = create_backend('hermes', self.spec)
        for i in range(14):
            agent.respond(str(i))
        self.assertEqual(len(agent.history), 24)
        agent.last_turn = -9999
        agent.respond('fresh')
        self.assertEqual(len(self.requests[-1][2]['messages']), 2)


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.backend = Mock()
        self.backend.respond.return_value = Reply('Agent answer', 'hermes')
        self.alerts = Mock()
        self.alerts.respond.return_value = Reply('Saved timer', 'local_alerts')
        self.router = Router({'backend': 'hermes'}, self.backend, self.alerts)

    def test_alerts_and_invalid_alerts_never_reach_remote_agent(self):
        for text in ('set a timer for five seconds', 'remind me to buy milk', 'set an alarm for banana', 'stop the alarm', 'cancel all timers'):
            self.router.respond(text, 'id')
        self.backend.respond.assert_not_called()
        self.assertEqual(self.alerts.respond.call_count, 5)
        self.alerts.respond.side_effect = BackendError('Local service unavailable')
        result = self.router.respond('set a timer for five seconds', 'id')
        self.assertFalse(result.success)
        self.backend.respond.assert_not_called()

    def test_questions_and_home_control_go_to_selected_agent(self):
        for text in ('what is an alarm clock', 'turn on kitchen lights', 'search the web for recent news'):
            self.router.respond(text, 'id')
        self.assertEqual(self.backend.respond.call_count, 3)
        self.alerts.respond.assert_not_called()

    def test_no_silent_fallback_after_agent_failure(self):
        self.backend.respond.side_effect = BackendError('Timed out; check before retrying')
        self.assertFalse(self.router.respond('turn on lights', 'id').success)
        self.assertEqual(self.backend.respond.call_count, 1)
        self.alerts.respond.assert_not_called()

    def test_reset_and_invalid_input(self):
        self.router.respond('new conversation', 'id')
        self.backend.reset.assert_called_once()
        for text in ('', 'x' * 4001, 123):
            self.assertFalse(self.router.respond(text, 'id').success)
        self.backend.respond.assert_not_called()


class VoiceControlTests(unittest.TestCase):
    def setUp(self):
        self.backend = Mock()
        self.backend.respond.return_value = Reply('Hello from Hermes', 'hermes')
        router = Router({'backend': 'hermes'}, self.backend, Mock())
        self.speaker, self.stt = Mock(), Mock()
        self.stt.transcribe.return_value = 'hello'
        self.coordinator = Coordinator(router, self.stt, self.speaker)
        runtime = Mock(coordinator=self.coordinator)
        runtime.health.return_value = {'backend': 'hermes', 'state': 'listening'}
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler(runtime, 'control-test-key'))
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def request(self, path, data, auth='control-test-key'):
        from urllib.request import Request, urlopen
        req = Request(f'http://127.0.0.1:{self.server.server_port}{path}', data=data,
                      headers={'Authorization': 'Bearer ' + auth})
        with urlopen(req, timeout=3) as response:
            return json.load(response)

    def test_authenticated_text_request_optional_speech(self):
        from urllib.error import HTTPError
        with self.assertRaises(HTTPError) as caught:
            self.request('/ask', b'{"text":"hello"}', auth='wrong')
        self.assertEqual(caught.exception.code, 401)
        caught.exception.close()
        result = self.request('/ask', b'{"text":"hello", "speak":true}')
        self.assertEqual(result['backend'], 'hermes')
        self.assertTrue(result['spoken'])
        self.speaker.say.assert_called_once_with('Hello from Hermes')

    def test_audio_runs_through_stt_and_same_router(self):
        result = self.request('/audio', b'\0\0' * 1600)
        self.stt.transcribe.assert_called_once()
        self.backend.respond.assert_called_once_with('hello')
        self.assertTrue(result['spoken'])

    def test_silent_audio_never_calls_agent(self):
        self.stt.transcribe.return_value = ''
        result = self.request('/audio', b'\0\0' * 1600)
        self.assertFalse(result['success'])
        self.backend.respond.assert_not_called()

    def test_spoken_error_does_not_claim_success(self):
        self.backend.respond.side_effect = BackendError('Agent unavailable')
        result = self.coordinator.text('hello', speak=True)
        self.assertFalse(result['success'])
        self.assertTrue(result['spoken'])
        self.speaker.say.assert_called_once_with('Agent unavailable')

    def test_last_turn_diagnostic_is_authenticated_and_expires(self):
        from urllib.error import HTTPError
        self.coordinator.text('open office blinds')
        with self.assertRaises(HTTPError) as caught:
            self.request('/last-turn', None, auth='wrong')
        caught.exception.close()
        detail = self.request('/last-turn', None)['last_turn']
        self.assertEqual(detail['heard'], 'open office blinds')
        self.assertEqual(detail['reply'], 'Hello from Hermes')
        self.coordinator.last_turn -= 301
        self.assertIsNone(self.request('/last-turn', None)['last_turn'])

    def test_mode_api_is_authenticated_and_validates_input(self):
        from urllib.error import HTTPError
        with self.assertRaises(HTTPError) as caught:
            self.request('/mode', b'{"mode":"quiet"}', auth='wrong')
        caught.exception.close()
        self.assertEqual(self.request('/mode', b'{"mode":"quiet"}')['response_mode'], 'quiet')
        self.assertEqual(self.request('/mode', None)['response_mode'], 'quiet')
        with self.assertRaises(HTTPError) as caught:
            self.request('/mode', b'{"mode":"mute-all"}')
        self.assertEqual(caught.exception.code, 400)
        caught.exception.close()
        self.assertEqual(self.coordinator.response_mode, 'quiet')

    def test_busy_cleared_after_recognition_error(self):
        self.stt.transcribe.side_effect = RuntimeError('test')
        with self.assertRaises(RuntimeError):
            self.coordinator.audio(b'data')
        self.assertFalse(self.coordinator.busy.is_set())


class BackendSwitchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'voice.json'
        self.secret = Path(self.tmp.name) / 'agent.json'
        self.secret.write_text(json.dumps({'api_key': 'private-test-token'}))
        self.config = {'backend': 'hermes', 'backends': {
            'hermes': {'type': 'openai', 'base_url': 'http://127.0.0.1:8642/v1', 'credentials_file': str(self.secret)},
            'replacement': {'type': 'openai', 'base_url': 'http://127.0.0.1:9999/v1', 'credentials_file': str(self.secret)},
            'homeassistant': {'type': 'homeassistant', 'base_url': 'http://127.0.0.1:8123', 'credentials_file': str(Path(self.tmp.name) / 'missing.json')},
        }}
        self.path.write_text(json.dumps(self.config))

    def tearDown(self):
        self.tmp.cleanup()

    def select(self, name):
        return subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / 'scripts/voicectl.py'),
            '--config', str(self.path), 'backend', name, '--no-restart'], capture_output=True, text=True)

    def test_replacement_agent_can_be_selected_using_config_only(self):
        result = self.select('replacement')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.path.read_text())['backend'], 'replacement')
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('private-test-token', result.stdout + result.stderr)

    def test_missing_ha_credentials_does_not_replace_working_backend(self):
        result = self.select('homeassistant')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(self.path.read_text())['backend'], 'hermes')

    def test_native_ha_can_be_selected_after_credentials_are_provided(self):
        self.config['backends']['homeassistant']['credentials_file'] = str(self.secret)
        self.path.write_text(json.dumps(self.config))
        result = self.select('homeassistant')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.path.read_text())['backend'], 'homeassistant')


class QuietModeTests(unittest.TestCase):
    def setUp(self):
        self.router = Mock()
        self.speaker = Mock()
        self.save = Mock()
        self.coordinator = Coordinator(self.router, Mock(), self.speaker, 'quiet', self.save)

    def test_successful_action_is_silent_but_text_is_preserved(self):
        self.router.respond.return_value = Reply('Window closed.', 'hermes', True, 'action_success')
        result = self.coordinator.text('close office blinds', speak=True)
        self.assertTrue(result['success'])
        self.assertTrue(result['speech_suppressed'])
        self.assertFalse(result['spoken'])
        self.assertEqual(result['text'], 'Window closed.')
        self.speaker.say.assert_not_called()

    def test_answers_clarifications_errors_and_unknown_responses_are_spoken(self):
        for kind, success in [('answer', True), ('clarification', True), ('error', False), ('unknown', True), ('action_success', False)]:
            self.speaker.reset_mock()
            self.router.respond.return_value = Reply('Important reply.', 'hermes', success, kind)
            result = self.coordinator.text('request', speak=True)
            self.assertTrue(result['spoken'])
            self.assertFalse(result['speech_suppressed'])
            self.speaker.say.assert_called_once_with('Important reply.')

    def test_question_phrased_as_command_uses_response_category(self):
        self.router.respond.return_value = Reply('Tokyo.', 'hermes', True, 'answer')
        self.assertTrue(self.coordinator.text('tell me the capital of Japan', speak=True)['spoken'])

    def test_local_alert_success_is_silent_but_list_is_spoken(self):
        self.router.respond.return_value = Reply('Timer saved.', 'local_alerts', True, 'action_success')
        self.assertTrue(self.coordinator.text('set a timer for a minute', speak=True)['speech_suppressed'])
        self.router.respond.return_value = Reply('One minute remains.', 'local_alerts', True, 'answer')
        self.assertTrue(self.coordinator.text('how much time is left', speak=True)['spoken'])

    def test_talkative_mode_speaks_successful_action(self):
        self.coordinator.response_mode = 'talkative'
        self.router.respond.return_value = Reply('Done.', 'hermes', True, 'action_success')
        self.assertTrue(self.coordinator.text('close window', speak=True)['spoken'])

    def test_voice_mode_change_is_saved_and_never_sent_to_agent(self):
        result = self.coordinator.text('enable talkative mode', speak=True)
        self.assertTrue(result['spoken'])
        self.save.assert_called_with('talkative')
        self.assertTrue(self.coordinator.text('enable quiet mode', speak=True)['speech_suppressed'])
        self.save.assert_called_with('quiet')
        self.router.respond.assert_not_called()

    def test_mode_save_failure_is_spoken_and_keeps_previous_setting(self):
        self.save.side_effect = OSError('write failed')
        result = self.coordinator.text('enable talkative mode', speak=True)
        self.assertFalse(result['success'])
        self.assertTrue(result['spoken'])
        self.assertEqual(self.coordinator.response_mode, 'quiet')

    def test_mode_persistence_preserves_backend_and_private_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'voice.json'
            path.write_text(json.dumps({'backend': 'hermes', 'wake': {'model': 'hey_jarvis'}}))
            runtime = VoiceRuntime.__new__(VoiceRuntime)
            runtime.config_path, runtime.config = path, json.loads(path.read_text())
            runtime.save_mode('quiet')
            saved = json.loads(path.read_text())
            self.assertEqual(saved['response_mode'], 'quiet')
            self.assertEqual(saved['backend'], 'hermes')
            self.assertEqual(saved['wake']['model'], 'hey_jarvis')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            restored = Coordinator(Mock(), Mock(), Mock(), saved['response_mode'])
            self.assertEqual(restored.response_mode, 'quiet')

    def test_mode_is_reported_as_an_answer(self):
        result = self.coordinator.text('what mode are you in', speak=True)
        self.assertEqual(result['response_type'], 'answer')
        self.assertTrue(result['spoken'])
        self.router.respond.assert_not_called()


if __name__ == '__main__':
    unittest.main()
