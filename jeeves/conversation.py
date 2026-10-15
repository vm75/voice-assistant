"""Text routing independent of capture, STT, TTS and agent implementations."""
import json
from pathlib import Path
import re

from .backends import BackendError, Reply, create_backend
from .backends.http import post_json


def is_alert_command(text):
    # Broad prefix matching keeps malformed local schedule requests local too.
    # They must return validation errors instead of being scheduled by an agent.
    return bool(re.match(
        r'^(?:(?:set|start)\s+(?:(?:a|an)\s+)?(?:(?:local|jeeves)\s+)?(?:timer|alarm)\b'
        r'|wake me up\b|alarm at\b|remind me\b'
        r'|(?:stop|cancel)\s+(?:(?:all|the)\s+)?(?:alarms?|timers?|reminders?|alerts)\b'
        r'|dismiss alerts\b|list (?:my )?alerts\b|what alerts are scheduled\b|how much time is left\b)',
        text.strip(), re.I))


class AlertClient:
    def __init__(self, config_path):
        try:
            config = json.loads(Path(config_path).expanduser().read_text())
            self.url = f'http://127.0.0.1:{int(config["port"])}/command'
            self.token = config['token']
        except (OSError, ValueError, KeyError):
            raise ValueError('Local alerts configuration is missing or invalid.') from None

    def respond(self, text, request_id):
        # HTTP 400 validation responses are safe to speak; do not redirect to agent.
        from urllib.error import HTTPError
        from urllib.request import Request, urlopen
        request = Request(self.url, data=json.dumps({'command': text, 'request_id': request_id}).encode(),
                          headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
        try:
            with urlopen(request, timeout=10) as response:
                result = json.load(response)
            kind = 'action_success' if 'alert' in result or 'count' in result else 'answer' if 'alerts' in result else 'unknown'
            return Reply(result['speech'], 'local_alerts', True, kind)
        except HTTPError as exc:
            try:
                result = json.loads(exc.read(8192))
                if exc.code == 400 and isinstance(result.get('speech'), str):
                    return Reply(result['speech'], 'local_alerts', False, 'error')
            except (ValueError, AttributeError):
                pass
            finally:
                exc.close()
            raise BackendError('Local alert service failed. The request is not confirmed; check active alerts before retrying.') from None
        except (OSError, ValueError, KeyError):
            raise BackendError('Local alert service is unavailable. The request is not confirmed; check active alerts before retrying.') from None


class Router:
    def __init__(self, config, backend=None, alerts=None):
        self.backend_name = config.get('backend', 'hermes')
        self.backend = backend or create_backend(self.backend_name, config['backends'][self.backend_name])
        self.alerts = alerts or AlertClient(config['alerts_config'])

    def respond(self, text, request_id):
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            return Reply('Please give a request of between 1 and 4000 characters.', 'local', False)
        text = text.strip()
        if text.lower().rstrip('.!?') in {'new conversation', 'forget this conversation'}:
            self.backend.reset()
            return Reply('Started a new conversation.', 'local', True, 'action_success')
        try:
            if is_alert_command(text):
                return self.alerts.respond(text, request_id)
            return self.backend.respond(text)
        except BackendError as exc:
            return Reply(str(exc), 'local_alerts' if is_alert_command(text) else self.backend_name, False)
