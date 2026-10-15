"""Standalone Jeeves voice pipeline; Hermes is the default conversation backend."""
import argparse
from contextlib import contextmanager
from dataclasses import asdict
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import os
import tempfile
import signal
import subprocess
import threading
import time
import uuid

from .conversation import Router
from .backends import Reply
from .voice_audio import Microphone, PiperSpeaker, SpeechRecorder, WakeDetector, WhisperSTT

LOG = logging.getLogger('jeeves.voice')


class Coordinator:
    def __init__(self, router, transcriber, speaker, response_mode="talkative", save_mode=None):
        self.router, self.transcriber, self.speaker = router, transcriber, speaker
        if response_mode not in {"quiet", "talkative"}:
            raise ValueError("response_mode must be quiet or talkative")
        self.response_mode, self.save_mode = response_mode, save_mode
        self.busy = threading.Event()
        self.lock = threading.Lock()
        self.last_error = None
        self.last_turn = None
        self.last_exchange = None

    @contextmanager
    def interaction(self):
        with self.lock:
            self.busy.set()
            try:
                yield
            finally:
                self.busy.clear()

    def set_mode(self, mode):
        if mode not in {'quiet', 'talkative'}:
            raise ValueError('Mode must be quiet or talkative.')
        if self.save_mode:
            self.save_mode(mode)
        self.response_mode = mode
        return Reply(mode.capitalize() + ' mode enabled.', 'local', True, 'action_success')

    def answer(self, text, speak):
        normalized = text.strip().lower().rstrip('.!?') if isinstance(text, str) else ''
        modes = {'enable quiet mode': 'quiet', 'quiet mode': 'quiet', 'be less chatty': 'quiet',
                 'enable talkative mode': 'talkative', 'talkative mode': 'talkative',
                 'disable quiet mode': 'talkative'}
        if normalized in modes:
            try:
                reply = self.set_mode(modes[normalized])
            except (OSError, ValueError):
                reply = Reply('I could not save the response mode. Please check the local configuration.', 'local', False, 'error')
        elif normalized in {'what mode are you in', 'what is your response mode'}:
            reply = Reply('I am in ' + self.response_mode + ' mode.', 'local', True, 'answer')
        else:
            reply = self.router.respond(text, uuid.uuid4().hex)
        result = asdict(reply)
        result['spoken'] = False
        result['speech_suppressed'] = bool(speak and self.response_mode == 'quiet'
                                          and reply.success and reply.response_type == 'action_success')
        if speak and not result['speech_suppressed']:
            try:
                self.speaker.say(reply.text)
                result['spoken'] = True
                self.last_error = None
            except (OSError, ValueError, TimeoutError, subprocess.SubprocessError):
                # No exception details/transcripts/credentials in the logs/API.
                self.last_error = 'Speech playback failed. Check the Jabra and local Piper.'
                result['audio_error'] = self.last_error
                LOG.error(self.last_error)
        self.last_turn = time.time()
        self.last_exchange = {'heard': text, 'reply': reply.text, 'backend': reply.backend,
                              'success': reply.success, 'spoken': result['spoken'],
                              'response_type': reply.response_type, 'speech_suppressed': result['speech_suppressed'],
                              'time': self.last_turn}
        return result

    def last_turn_details(self):
        # A single diagnostic, authenticated and RAM-only, expires after five minutes.
        if not self.last_turn or time.time() - self.last_turn > 300:
            self.last_exchange = None
        return self.last_exchange

    def text(self, text, speak=False):
        with self.interaction():
            return self.answer(text, speak)

    def audio(self, pcm, speak=True):
        with self.interaction():
            text = self.transcriber.transcribe(pcm)
            if not text:
                return {'text': 'No speech was recognized.', 'backend': 'local', 'success': False, 'spoken': False}
            # Transcripts stay in RAM and are deliberately not included in logs.
            return self.answer(text, speak)


class VoiceRuntime:
    def __init__(self, config, config_path=None):
        self.config = config
        self.config_path = config_path
        self.stop = threading.Event()
        self.state = 'loading_models'
        self.last_error = None
        self.detector = WakeDetector(config.get('wake', {}))
        self.recorder = SpeechRecorder(config.get('recording', {}))
        self.coordinator = Coordinator(Router(config), WhisperSTT(config['stt']),
                                       PiperSpeaker(config['alerts_config'], self.stop),
                                       config.get('response_mode', 'talkative'), self.save_mode)
        self.worker = threading.Thread(target=self.listen, daemon=True)

    def save_mode(self, mode):
        if self.config_path:
            # Reread to preserve unrelated settings; credentials remain outside the repo.
            config = json.loads(self.config_path.read_text())
            config['response_mode'] = mode
            fd, temporary = tempfile.mkstemp(dir=self.config_path.parent)
            try:
                with os.fdopen(fd, 'w') as output:
                    json.dump(config, output, indent=2)
                os.replace(temporary, self.config_path)
            finally:
                Path(temporary).unlink(missing_ok=True)
        self.config['response_mode'] = mode

    def listen(self):
        while not self.stop.is_set():
            try:
                if self.coordinator.busy.is_set():
                    self.state = 'processing'
                    self.stop.wait(.1)
                    continue
                self.detector.reset()
                self.state = 'opening_microphone'
                woke = False
                with Microphone(self.config['source'], self.stop) as microphone:
                    self.state = 'listening'
                    LOG.info('Listening for wake word; conversation backend=%s', self.coordinator.router.backend_name)
                    while not self.stop.is_set() and not self.coordinator.busy.is_set():
                        if self.detector.detected(microphone.read(2560)):
                            woke = True
                            break
                if not woke:
                    self.stop.wait(.1)
                    continue
                # Stop capture while cue and response play to avoid self-triggering.
                with self.coordinator.interaction():
                    self.state = 'recording'
                    self.coordinator.speaker.cue()
                    with Microphone(self.config['source'], self.stop) as microphone:
                        pcm = self.recorder.record(microphone)
                    if pcm:
                        self.state = 'processing'
                        text = self.coordinator.transcriber.transcribe(pcm)
                        if text:
                            result = self.coordinator.answer(text, speak=True)
                            LOG.info('Turn finished backend=%s success=%s spoken=%s',
                                     result['backend'], result['success'], result['spoken'])
                self.last_error = None
                self.stop.wait(1)  # refractory interval after speaker output
            except InterruptedError:
                break
            except Exception:
                # Exception strings from external libraries may contain request data.
                self.state = 'retrying'
                self.last_error = 'Voice capture, recognition or playback failed; retrying.'
                LOG.error(self.last_error)
                self.stop.wait(3)
        self.state = 'stopped'

    def health(self):
        return {'backend': self.coordinator.router.backend_name, 'state': self.state,
                'response_mode': self.coordinator.response_mode,
                'listener_alive': self.worker.is_alive(), 'busy': self.coordinator.busy.is_set(),
                'last_error': self.last_error or self.coordinator.last_error,
                'last_turn': self.coordinator.last_turn,
                'wake_model': self.config.get('wake', {}).get('model_path') or self.config.get('wake', {}).get('model', 'hey_jarvis')}


def handler(runtime, token):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *_):
            pass  # Never log input bodies, authorization or transcripts.

        def reply(self, code, result):
            raw = json.dumps(result).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def authorized(self):
            supplied = self.headers.get('Authorization', '').encode()
            if not hmac.compare_digest(supplied, ('Bearer ' + token).encode()):
                self.reply(401, {'error': 'Unauthorized'})
                return False
            return True

        def do_GET(self):
            if self.authorized():
                if self.path == '/health':
                    self.reply(200, runtime.health())
                elif self.path == '/mode':
                    self.reply(200, {'response_mode': runtime.coordinator.response_mode})
                elif self.path == '/last-turn':
                    self.reply(200, {'last_turn': runtime.coordinator.last_turn_details()})
                else:
                    self.reply(404, {'error': 'Not found'})

        def do_POST(self):
            if not self.authorized():
                return
            try:
                length = int(self.headers.get('Content-Length', 0))
                if self.path == '/mode':
                    if not 0 < length <= 1024:
                        raise ValueError('Mode body too large.')
                    data = json.loads(self.rfile.read(length))
                    if not isinstance(data, dict):
                        raise ValueError('JSON object required.')
                    with runtime.coordinator.interaction():
                        runtime.coordinator.set_mode(data.get('mode'))
                    result = {'response_mode': runtime.coordinator.response_mode}
                elif self.path == '/ask':
                    if not 0 < length <= 32768:
                        raise ValueError('JSON request must be 1–32768 bytes.')
                    data = json.loads(self.rfile.read(length))
                    if not isinstance(data, dict) or not isinstance(data.get('text'), str) or type(data.get('speak', False)) is not bool:
                        raise ValueError('Provide text and optional boolean speak.')
                    result = runtime.coordinator.text(data['text'], data.get('speak', False))
                elif self.path == '/audio':
                    # Local test/PTT input: raw mono S16_LE at 16kHz, max 60 seconds.
                    if not 0 < length <= 16000 * 2 * 60 or length % 2:
                        raise ValueError('PCM must be 16-bit mono 16kHz, at most 60 seconds.')
                    result = runtime.coordinator.audio(self.rfile.read(length), speak=True)
                else:
                    self.reply(404, {'error': 'Not found'})
                    return
                self.reply(200, result)
            except (ValueError, TypeError):
                self.reply(400, {'error': 'Invalid request body or audio format.'})
            except Exception:
                LOG.error('Voice request processing failed.')
                self.reply(500, {'error': 'Voice request failed; check local voice service health.'})
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    config = json.loads(args.config.read_text())
    if len(config['control_token']) < 32:
        raise ValueError('Voice control_token must have at least 32 characters.')
    runtime = VoiceRuntime(config, args.config)
    server = ThreadingHTTPServer(('127.0.0.1', int(config.get('port', 5125))), handler(runtime, config['control_token']))
    def stop(*_):
        runtime.stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    runtime.worker.start()
    LOG.info('Voice control ready on loopback port %s; backend=%s', server.server_port, runtime.coordinator.router.backend_name)
    try:
        server.serve_forever()
    finally:
        runtime.stop.set()
        server.server_close()
        runtime.worker.join(timeout=5)


if __name__ == '__main__':
    main()
