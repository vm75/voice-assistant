"""Replaceable local audio, wake-word, recognition and speech components."""
import json
import os
from pathlib import Path
import select
import subprocess
import tempfile
import time
import wave

from .alerts import chime

RATE = 16000
FRAME_BYTES = 640  # WebRTC VAD: 20ms, mono, signed 16-bit little endian.


class Microphone:
    def __init__(self, source, stop):
        self.source, self.stop = source, stop
        self.process = None
        self.buffer = bytearray()

    def __enter__(self):
        sources = json.loads(subprocess.check_output(['pactl', '--format=json', 'list', 'sources'], timeout=5))
        if self.source not in {s['name'] for s in sources}:
            raise OSError('Configured Jabra microphone is unavailable.')
        self.process = subprocess.Popen([
            'parec', '--raw', '--format=s16le', '--rate=16000', '--channels=1',
            '--latency-msec=40', '--client-name=jeeves-voice', '--device=' + self.source,
        ], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env={**os.environ, 'PULSE_PROP': 'node.dont-fallback=true node.dont-move=true'})
        return self

    def read(self, size):
        deadline = time.monotonic() + 5
        while len(self.buffer) < size:
            if self.stop.is_set():
                raise InterruptedError('Voice service is stopping.')
            if time.monotonic() > deadline:
                raise OSError('Microphone stopped delivering audio.')
            ready, _, _ = select.select([self.process.stdout], [], [], .2)
            if not ready:
                continue
            chunk = os.read(self.process.stdout.fileno(), 4096)
            if not chunk:
                raise OSError('Microphone stream closed.')
            self.buffer.extend(chunk)
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result

    def __exit__(self, *_):
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            self.process.stdout.close()


class WakeDetector:
    def __init__(self, config):
        from pyopen_wakeword import Model, OpenWakeWord, OpenWakeWordFeatures
        path = config.get('model_path')
        if path and not Path(path).is_file():
            raise ValueError('Configured custom wake model is missing.')
        self.model = OpenWakeWord.from_model(path) if path else OpenWakeWord.from_builtin(Model(config.get('model', 'hey_jarvis')))
        self.features = OpenWakeWordFeatures.from_builtin()
        self.threshold = float(config.get('threshold', .5))
        if not 0 < self.threshold < 1:
            raise ValueError('Wake threshold must be between zero and one.')

    def detected(self, pcm):
        return any(prob >= self.threshold for features in self.features.process_streaming(pcm)
                   for prob in self.model.process_streaming(features))

    def reset(self):
        self.model.reset()
        self.features.reset()


class SpeechRecorder:
    """Wait for speech, retain pre-roll, stop on silence or a bounded duration."""
    def __init__(self, config):
        import webrtcvad
        self.vad = webrtcvad.Vad(int(config.get('vad_mode', 2)))
        self.max_seconds = float(config.get('max_seconds', 20))
        self.silence_seconds = float(config.get('silence_seconds', 1.0))
        self.wait_seconds = float(config.get('wait_seconds', 5))
        if not (1 <= self.max_seconds <= 60 and .3 <= self.silence_seconds <= 5 and 1 <= self.wait_seconds <= 30):
            raise ValueError('Invalid speech recorder time limits.')

    def record(self, microphone):
        from collections import deque
        pre_roll = deque(maxlen=15)
        audio = bytearray()
        heard = False
        quiet = 0
        # Durations follow audio frames, not processing speed.
        for index in range(int((self.wait_seconds + self.max_seconds) / .02)):
            frame = microphone.read(FRAME_BYTES)
            speech = self.vad.is_speech(frame, RATE)
            if not heard:
                pre_roll.append(frame)
                if speech:
                    heard = True
                    audio.extend(b''.join(pre_roll))
                elif index * .02 >= self.wait_seconds:
                    return b''
                continue
            audio.extend(frame)
            quiet = 0 if speech else quiet + 1
            if quiet * .02 >= self.silence_seconds or len(audio) / (RATE * 2) >= self.max_seconds:
                break
        return bytes(audio)


class WhisperSTT:
    def __init__(self, config):
        from faster_whisper import WhisperModel
        self.language = config.get('language', 'en')
        self.model = WhisperModel(config['model_path'], device='cpu', compute_type='int8',
                                  cpu_threads=int(config.get('cpu_threads', 2)), num_workers=1,
                                  local_files_only=True)

    def transcribe(self, pcm):
        import numpy as np
        audio = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768.0
        if not len(audio) or float(np.max(np.abs(audio))) < .002:
            return ''
        segments, _ = self.model.transcribe(audio, language=self.language, beam_size=1,
            condition_on_previous_text=False, vad_filter=True,
            vad_parameters={'min_silence_duration_ms': 500},
            no_speech_threshold=.6)
        return ' '.join(s.text.strip() for s in segments).strip()


def read_wav(path):
    with wave.open(str(path), 'rb') as source:
        if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (1, 2, RATE):
            raise ValueError('Audio must be mono, 16-bit PCM WAV at 16000 Hz.')
        if source.getnframes() > RATE * 60:
            raise ValueError('Audio must be at most 60 seconds.')
        return source.readframes(source.getnframes())


class PiperSpeaker:
    def __init__(self, alerts_config, stop=None):
        self.config = json.loads(Path(alerts_config).expanduser().read_text())
        self.stop = stop

    def _play(self, path):
        sink = self.config['sink']
        sinks = json.loads(subprocess.check_output(['pactl', '--format=json', 'list', 'sinks'], timeout=5))
        if sink not in {s['name'] for s in sinks}:
            raise OSError('Jabra speaker is unavailable; refusing audio fallback.')
        with subprocess.Popen(['mpv', '--no-config', '--no-video', '--really-quiet',
                               '--audio-device=pulse/' + sink, str(path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                env={**os.environ, 'PULSE_PROP': 'node.dont-fallback=true node.dont-move=true'}) as player:
            deadline = time.monotonic() + 120
            while True:
                try:
                    _, error = player.communicate(timeout=.2)
                    if player.returncode:
                        raise OSError('Jabra playback failed.')
                    return
                except subprocess.TimeoutExpired:
                    if (self.stop and self.stop.is_set()) or time.monotonic() >= deadline:
                        player.kill()
                        player.communicate()
                        raise OSError('Speech playback interrupted or timed out.')

    def cue(self):
        with tempfile.TemporaryDirectory(prefix='jeeves-cue-') as tmp:
            path = Path(tmp) / 'cue.wav'
            chime(path)
            self._play(path)

    def say(self, text):
        text = text.strip()
        if len(text) > 1200:
            text = text[:1200].rsplit(' ', 1)[0] + '. The spoken answer was shortened.'
        with tempfile.TemporaryDirectory(prefix='jeeves-speech-') as tmp:
            output = Path(tmp) / 'reply.wav'
            tts = self.config['tts']
            subprocess.run([tts['executable'], '--model', tts['model'], '--output-file', str(output)],
                input=text, text=True, capture_output=True, check=True, timeout=30)
            self._play(output)
