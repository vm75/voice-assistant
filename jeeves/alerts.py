"""Local scheduler and authenticated HTTP API. Python standard library only."""
import argparse
import hmac
import json
import logging
import math
import os
from pathlib import Path
import re
import signal
import sqlite3
import struct
import subprocess
import tempfile
import threading
import time
import uuid
import wave
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

LOG = logging.getLogger("jeeves")
NUMBERS = dict(zip("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split(), range(20)))
NUMBERS.update(dict(zip("twenty thirty forty fifty sixty seventy eighty ninety".split(), range(20, 100, 10))))


def number(text):
    text = text.strip().replace("-", " ")
    if text.isdigit():
        return int(text)
    words = text.split()
    if words == ["a"] or words == ["an"]:
        return 1
    if len(words) == 1 and words[0] in NUMBERS:
        return NUMBERS[words[0]]
    if len(words) == 2 and NUMBERS.get(words[0], 0) >= 20 and 0 < NUMBERS.get(words[1], 100) < 10:
        return NUMBERS[words[0]] + NUMBERS[words[1]]
    raise ValueError("Use a number from one to ninety nine, or digits.")


def duration(text):
    """Parse whole seconds/minutes/hours/days, including combined units."""
    total = 0
    rest = text.lower().strip()
    pattern = r"\s*(?:and\s+)?([a-z\d -]+?)\s+(seconds?|minutes?|hours?|days?)\b"
    while rest:
        match = re.match(pattern, rest)
        if not match:
            raise ValueError("Use a duration such as five minutes or 1 hour and 30 minutes.")
        total += number(match[1]) * {"second": 1, "minute": 60, "hour": 3600, "day": 86400}[match[2].rstrip("s")]
        rest = rest[match.end():].strip()
    if not 0 < total <= 366 * 86400:
        raise ValueError("Duration must be positive and at most 366 days.")
    return total


def alarm_time(text, zone, now):
    """Next local occurrence. Reject nonexistent/ambiguous DST wall times."""
    if "T" in text:
        try:
            absolute = datetime.fromisoformat(text.strip())
            if absolute.tzinfo is None or absolute.timestamp() <= now:
                raise ValueError("Timestamp must include an offset and be in the future.")
            return absolute.timestamp()
        except ValueError as exc:
            raise ValueError("Use a future ISO timestamp with timezone offset.") from exc
    raw = text.lower().strip().replace(".", "")
    tomorrow = raw.startswith("tomorrow ")
    today = raw.startswith("today ")
    raw = re.sub(r"^(tomorrow|today)\s+", "", raw)
    match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", raw)
    if not match:
        # Spoken whole hours (seven AM).
        spoken = re.fullmatch(r"([a-z -]+)\s+(am|pm)", raw)
        if not spoken:
            raise ValueError("Use a time such as 7 AM, 19:30, or tomorrow 7 AM.")
        hour, minute, meridian = number(spoken[1]), 0, spoken[2]
    else:
        hour, minute, meridian = int(match[1]), int(match[2] or 0), match[3]
    if minute > 59 or (meridian and not 1 <= hour <= 12) or (not meridian and not 0 <= hour <= 23):
        raise ValueError("Invalid clock time.")
    if meridian:
        hour = hour % 12 + (12 if meridian == "pm" else 0)
    local = datetime.fromtimestamp(now, zone)
    day = local.date() + timedelta(days=int(tomorrow))
    candidate = datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute, tzinfo=zone)
    if not tomorrow and candidate.timestamp() <= now:
        if today:
            raise ValueError("That time today has already passed.")
        candidate += timedelta(days=1)
    back = datetime.fromtimestamp(candidate.timestamp(), zone)
    if back.replace(tzinfo=None) != candidate.replace(tzinfo=None):
        raise ValueError("That time does not exist due to daylight saving time. Choose another time.")
    if candidate.replace(fold=0).utcoffset() != candidate.replace(fold=1).utcoffset():
        raise ValueError("That time is ambiguous due to daylight saving time. Use an ISO timestamp with offset via the API.")
    return candidate.timestamp()


def parse_command(command, zone, now):
    text = command.strip().rstrip(".!?")
    low = text.lower()
    if re.fullmatch(r"stop (?:the )?(?:alarm|timer|reminder)", low) or low in {"stop alerts", "dismiss alerts", "stop all alerts"}:
        return {"action": "dismiss"}
    match = re.fullmatch(r"cancel (?:all |the )?(timers?|alarms?|reminders?)", low)
    if match:
        return {"action": "cancel", "kind": match[1].rstrip("s")}
    if low in {"list alerts", "list my alerts", "what alerts are scheduled", "how much time is left"}:
        return {"action": "list"}
    match = re.fullmatch(r"(?:set|start) (?:a |an )?(?:jeeves |local )?timer for (.+)", text, re.I)
    if match:
        return {"kind": "timer", "due": now + duration(match[1]), "text": "Your timer is finished"}
    match = re.fullmatch(r"(?:set (?:an? )?alarm for|wake me up at|alarm at) (.+)", text, re.I)
    if match:
        return {"kind": "alarm", "due": alarm_time(match[1], zone, now), "text": "Your alarm is ringing"}
    match = re.fullmatch(r"remind me (?:to )?(.+) (in|at) (.+)", text, re.I)
    if match:
        due = now + duration(match[3]) if match[2].lower() == "in" else alarm_time(match[3], zone, now)
        return {"kind": "reminder", "due": due, "text": match[1].strip()}
    raise ValueError("Try set a timer for five minutes, set an alarm for 7 AM, or remind me to call Sam in two hours.")


class Store:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS alerts (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, text TEXT NOT NULL,
            due REAL NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
            started REAL, last_play REAL NOT NULL DEFAULT 0,
            request_id TEXT UNIQUE)""")
        self.db.commit()

    def create(self, kind, text, due, request_id=None):
        if kind not in {"timer", "alarm", "reminder"} or not isinstance(text, str) or not 1 <= len(text) <= 500:
            raise ValueError("Invalid kind or text (1–500 characters).")
        if not isinstance(due, (int, float)) or isinstance(due, bool) or not math.isfinite(due):
            raise ValueError("Invalid due timestamp.")
        with self.lock, self.db:
            if request_id:
                old = self.db.execute("SELECT * FROM alerts WHERE request_id=?", (request_id,)).fetchone()
                if old:
                    return dict(old)
            if self.db.execute("SELECT count(*) FROM alerts WHERE state IN ('pending','ringing')").fetchone()[0] >= 100:
                raise ValueError("At most 100 active alerts are supported.")
            key = uuid.uuid4().hex[:12]
            self.db.execute("INSERT INTO alerts(id,kind,text,due,request_id) VALUES(?,?,?,?,?)", (key, kind, text, due, request_id))
            return dict(self.db.execute("SELECT * FROM alerts WHERE id=?", (key,)).fetchone())

    def active(self):
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM alerts WHERE state IN ('pending','ringing') ORDER BY due")]

    def stop(self, state, kind=None, key=None):
        with self.lock, self.db:
            sql = "UPDATE alerts SET state=? WHERE state IN ('pending','ringing')"
            args = [state]
            if state == "dismissed":
                sql += " AND state='ringing'"
            if kind:
                if kind not in {"timer", "alarm", "reminder"}:
                    raise ValueError("Invalid kind.")
                sql += " AND kind=?"
                args.append(kind)
            if key:
                sql += " AND id=?"
                args.append(key)
            return self.db.execute(sql, args).rowcount

    def tick(self, now, catchup=86400, ring_limit=900, repeat=15):
        with self.lock, self.db:
            self.db.execute("UPDATE alerts SET state='missed' WHERE state='pending' AND due<?", (now - catchup,))
            self.db.execute("UPDATE alerts SET state='ringing',started=? WHERE state='pending' AND due<=?", (now, now))
            self.db.execute("UPDATE alerts SET state='expired' WHERE state='ringing' AND started<=?", (now - ring_limit,))
            row = self.db.execute("SELECT * FROM alerts WHERE state='ringing' AND last_play<=? ORDER BY last_play,due LIMIT 1", (now - repeat,)).fetchone()
            if row:
                self.db.execute("UPDATE alerts SET last_play=? WHERE id=?", (now, row["id"]))
                return dict(row)
        return None

    def ringing(self, key):
        with self.lock:
            return self.db.execute("SELECT state FROM alerts WHERE id=?", (key,)).fetchone()[0] == "ringing"


def chime(path):
    rate = 22050
    samples = []
    for i in range(rate):
        t = i / rate
        envelope = max(0, 1 - (t % 0.5) * 2) * min(1, (t % 0.5) * 100)
        samples.append(int(14000 * envelope * math.sin(2 * math.pi * (880 if t < .5 else 1100) * t)))
    with wave.open(str(path), "wb") as wav:
        wav.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        wav.writeframes(struct.pack("<" + "h" * len(samples), *samples))


class Audio:
    def __init__(self, sink, tts=None):
        self.sink = sink
        root = Path(__file__).resolve().parent.parent
        self.tts = tts or {
            "executable": str(root / "vendor/piper-tts/.venv/bin/piper"),
            "model": str(root / "models/tts/en_US-lessac-high.onnx"),
        }
        self.last_error = None
        self.last_success = None
        self.process = None
        self.current = None
        self.lock = threading.RLock()

    def stop_if(self, predicate):
        with self.lock:
            if self.current and predicate(self.current) and self.process and self.process.poll() is None:
                self.process.terminate()

    def play(self, alert, store):
        try:
            sinks = json.loads(subprocess.check_output(
                ["pactl", "--format=json", "list", "sinks"], timeout=5))
            if self.sink not in {sink["name"] for sink in sinks}:
                raise RuntimeError("Configured Jabra sink is unavailable; refusing audio fallback")
            with tempfile.TemporaryDirectory(prefix="jeeves-") as tmp:
                tone, speech = Path(tmp) / "chime.wav", Path(tmp) / "speech.wav"
                chime(tone)
                speech_error = None
                files = [str(tone)]
                try:
                    subprocess.run(
                        [self.tts["executable"], "--model", self.tts["model"],
                         "--output-file", str(speech)],
                        input=f'{alert["kind"]}. {alert["text"]}\n', text=True,
                        check=True, timeout=30, capture_output=True)
                    files.append(str(speech))
                except (OSError, subprocess.SubprocessError) as exc:
                    speech_error = "Speech synthesis failed; playing chime only: " + str(exc)
                    LOG.warning(speech_error)
                with self.lock:
                    if not store.ringing(alert["id"]):
                        return
                    self.current = alert
                    self.process = subprocess.Popen(
                        ["mpv", "--no-config", "--no-video", "--really-quiet",
                         f"--audio-device=pulse/{self.sink}", *files],
                        env={**os.environ, "PULSE_PROP": "node.dont-fallback=true node.dont-move=true"},
                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                try:
                    _, stderr = self.process.communicate(timeout=45)
                    if self.process.returncode and store.ringing(alert["id"]):
                        raise RuntimeError(stderr.decode(errors="replace")[-1000:])
                    if self.process.returncode == 0:
                        self.last_success = time.time()
                        self.last_error = speech_error
                        LOG.info("Playback completed for %s %s", alert["kind"], alert["id"])
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.communicate()
                    raise RuntimeError("Audio playback timed out")
                finally:
                    with self.lock:
                        self.current = None
                        self.process = None
        except Exception as exc:
            self.last_error = str(exc)
            LOG.exception("Audio failed for %s; will retry", alert["id"])


class Application:
    def __init__(self, store, audio, timezone, allowed_clients=None):
        self.store, self.audio, self.zone = store, audio, ZoneInfo(timezone)
        self.allowed_clients = allowed_clients or ["127.0.0.1"]
        self.shutdown = threading.Event()

    def command(self, data):
        command = data.get("command")
        if not isinstance(command, str) or not 1 <= len(command) <= 1000:
            raise ValueError("command must be a string of 1–1000 characters.")
        spec = parse_command(command, self.zone, time.time())
        action = spec.pop("action", "create")
        if action == "create":
            request_id = data.get("request_id")
            if request_id is not None and (not isinstance(request_id, str) or not 1 <= len(request_id) <= 200):
                raise ValueError("Invalid request_id.")
            row = self.store.create(**spec, request_id=request_id)
            when = datetime.fromtimestamp(row["due"], self.zone).strftime("%A %B %d at %I:%M:%S %p %Z")
            return {"alert": row, "speech": f'{row["kind"].capitalize()} saved for {when}. ID {row["id"]}.'}
        if action == "list":
            rows = self.store.active()
            details = []
            for row in rows:
                remaining = max(0, math.ceil(row["due"] - time.time()))
                details.append(f'{row["kind"]} {row["text"]}, {remaining} seconds remaining')
            return {"alerts": rows, "speech": "; ".join(details) or "No active alerts."}
        count = self.store.stop("dismissed" if action == "dismiss" else "cancelled", spec.get("kind"))
        self.audio.stop_if(lambda alert: not self.store.ringing(alert["id"]))
        return {"count": count, "speech": f'{count} alert(s) {"dismissed" if action == "dismiss" else "cancelled"}.'}

    def run_scheduler(self):
        while not self.shutdown.is_set():
            try:
                row = self.store.tick(time.time())
                if row:
                    LOG.info("Sounding %s %s", row["kind"], row["id"])
                    self.audio.play(row, self.store)
            except Exception:
                LOG.exception("Scheduler iteration failed")
            self.shutdown.wait(.5)


def handler(app, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            LOG.info("HTTP %s %s", self.client_address[0], fmt % args)

        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def reply(self, status, body):
            content = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def authorized(self):
            if self.client_address[0] not in app.allowed_clients:
                self.reply(403, {"error": "Client not allowed"})
                return False
            if not hmac.compare_digest(self.headers.get("Authorization", "").encode(), ("Bearer " + token).encode()):
                self.reply(401, {"error": "Unauthorized"})
                return False
            return True

        def do_GET(self):
            if not self.authorized():
                return
            if self.path == "/health":
                self.reply(200, {"scheduler_alive": app.worker.is_alive(), "timezone": str(app.zone), "audio_last_error": app.audio.last_error, "audio_last_success": app.audio.last_success})
            elif self.path == "/alerts":
                self.reply(200, {"alerts": app.store.active()})
            else:
                self.reply(404, {"error": "Not found"})

        def do_POST(self):
            if not self.authorized():
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 8192:
                    raise ValueError("JSON body must be 1–8192 bytes.")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("JSON object required.")
                if self.path == "/command":
                    result = app.command(data)
                elif self.path in {"/dismiss", "/cancel"}:
                    state = "dismissed" if self.path == "/dismiss" else "cancelled"
                    count = app.store.stop(state, data.get("kind"), data.get("id"))
                    app.audio.stop_if(lambda alert: not app.store.ringing(alert["id"]))
                    result = {"count": count}
                else:
                    self.reply(404, {"error": "Not found"})
                    return
                self.reply(200, result)
            except (ValueError, TypeError, KeyError) as exc:
                self.reply(400, {"error": str(exc), "speech": str(exc)})
            except Exception:
                LOG.exception("Request failed")
                self.reply(500, {"error": "Internal error", "speech": "The alert service failed. Please check its logs."})
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = json.loads(args.config.read_text())
    if len(config["token"]) < 32:
        raise ValueError("Use a random token of at least 32 characters.")
    state = Path(config["state_dir"]).expanduser()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    app = Application(Store(state / "alerts.sqlite3"), Audio(config["sink"], config.get("tts")), config["timezone"], config.get("allowed_clients"))
    server = ThreadingHTTPServer((config.get("host", "127.0.0.1"), config.get("port", 5124)), handler(app, config["token"]))
    app.worker = threading.Thread(target=app.run_scheduler, daemon=True)
    app.worker.start()
    def stop(*_):
        app.shutdown.set()
        app.audio.stop_if(lambda _: True)
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    LOG.info("Listening on %s:%s, timezone %s", *server.server_address, app.zone)
    try:
        server.serve_forever()
    finally:
        app.shutdown.set()
        server.server_close()
        app.worker.join(timeout=50)


if __name__ == "__main__":
    main()
