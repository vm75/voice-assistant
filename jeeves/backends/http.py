"""Hermes/other OpenAI-compatible agents and native HA conversation API."""
import html
import http.client
import json
from pathlib import Path
import re
import socket
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import uuid

from . import BackendError, Reply


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def post_json(url, token, payload, timeout=120, headers=None):
    """Never retry action-capable requests or forward credentials to redirects."""
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={
        'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
        'Accept': 'application/json', **(headers or {}),
    })
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise BackendError('The service returned too much data.')
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise BackendError('The service returned an invalid response.')
            return result
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        if code in {401, 403}:
            raise BackendError('Service authentication failed. Check its private credentials.') from None
        raise BackendError(f'The service returned HTTP {code}. The request is not confirmed; check before retrying.') from None
    except (TimeoutError, socket.timeout):
        raise BackendError('The service timed out. The action may have completed; check before retrying.') from None
    except urllib.error.URLError:
        raise BackendError('The service is unreachable. The request is not confirmed; check before retrying.') from None
    except (OSError, http.client.HTTPException):
        raise BackendError('The service connection was interrupted. The action may have completed; check before retrying.') from None
    except (ValueError, UnicodeError):
        raise BackendError('The service returned invalid JSON.') from None


def settings(specification):
    spec = dict(specification)
    secret = {}
    if spec.get('credentials_file'):
        try:
            secret = json.loads(Path(spec['credentials_file']).expanduser().read_text())
        except (OSError, ValueError):
            raise ValueError('Backend credentials file is missing or invalid; see docs/BACKENDS.md.') from None
    if not isinstance(secret, dict):
        raise ValueError('Backend credentials must be a JSON object.')
    url = spec.get('base_url', secret.get('base_url', ''))
    if not isinstance(url, str):
        raise ValueError('Backend base_url must be a string.')
    url = url.rstrip('/')
    parsed = urlsplit(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Backend base_url must be an HTTP(S) URL without embedded credentials or query.')
    token = secret.get('api_key', secret.get('token'))
    if not isinstance(token, str) or not token.strip():
        raise ValueError('Backend credentials file needs api_key (or token).')
    timeout = float(spec.get('timeout', 120))
    if not 1 <= timeout <= 600:
        raise ValueError('Backend timeout must be between 1 and 600 seconds.')
    return url, token, timeout


class OpenAIBackend:
    """One adapter for Hermes and replacement agents speaking Chat Completions."""
    def __init__(self, name, spec):
        self.name, self.spec = name, spec
        self.url, self.token, self.timeout = settings(spec)
        self.model = spec.get('model', 'hermes-agent')
        self.system = spec.get('system_prompt',
            'You are Jeeves, a voice assistant. Respond briefly in plain spoken English. '
            'Use your configured tools for home control and live web information when needed. '
            'Interpret room-labelled lights, fans, windows, blinds, curtains and covers as smart-home device requests. '
            'For device-control requests, first use your Home Assistant or configured smart-home tools to resolve '
            'the spoken name against live entity names, aliases, and areas, including cover entities for windows. '
            'Do not ask what a named device is just because it is absent from this conversation. '
            'If exactly one entity matches, perform the requested action with the appropriate service. '
            'Ask for clarification only after lookup finds no match or multiple plausible matches. '
            'Only claim an action succeeded after its tool confirms success. '
            'Do not read Markdown, URLs or internal tool output aloud. '
            'Local timers, alarms and reminders are managed separately on the satellite.')
        self.system += (
            '\nFor the voice frontend, format your FINAL answer as a JSON object with exactly '
            '"text" (the plain spoken reply) and "response_type" (one of '
            '"action_success", "answer", "clarification", "error"). '
            'Use action_success ONLY for a completed command confirmed by successful tool results, '
            'with no unanswered question, missing input, partial failure or pending action. '
            'Questions, including questions phrased as commands such as tell me or look up, are answer. '
            'A command combined with a question is answer. Requests needing more input are clarification. '
            'Failed, partially failed, uncertain or unconfirmed commands are error. '
            'Do not put tool calls in this JSON; execute tools normally before the final answer. '
            'This metadata controls whether a successful command confirmation is spoken.')
        self.reset()

    def reset(self):
        self.history = []
        self.session_id = uuid.uuid4().hex
        self.last_turn = 0

    def respond(self, text):
        if time.monotonic() - self.last_turn > self.spec.get('session_idle_seconds', 300):
            self.reset()
        headers = {}
        # Vendor-specific session support is configuration, not a voice dependency.
        if self.spec.get('session_header'):
            headers[self.spec['session_header']] = self.session_id
        if self.spec.get('session_key_header'):
            headers[self.spec['session_key_header']] = self.spec.get('session_key', 'jeeves-voice')
        messages = [{'role': 'system', 'content': self.system}, *self.history,
                    {'role': 'user', 'content': text}]
        result = post_json(self.url + '/chat/completions', self.token,
                           {'model': self.model, 'messages': messages, 'stream': False},
                           self.timeout, headers)
        try:
            choice = result['choices'][0]
            if choice.get('finish_reason') not in {None, 'stop'}:
                raise BackendError('The agent did not finish its response. Check the request before retrying.')
            message = choice['message']
            if message.get('tool_calls'):
                raise BackendError('This endpoint returned unexecuted tools. Connect to the agent API, not its underlying model.')
            content = message['content']
            if isinstance(content, list):
                content = ' '.join(p['text'] for p in content if p.get('type') == 'text')
            if not isinstance(content, str) or not content.strip():
                raise ValueError()
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            raise BackendError('The agent returned no usable spoken answer.') from None
        response_type = 'unknown'
        spoken_content = content.strip()
        # Unstructured/invalid metadata remains audible; never guess success from wording.
        try:
            structured = json.loads(spoken_content)
            if (isinstance(structured, dict) and isinstance(structured.get('text'), str)
                    and structured['text'].strip()):
                spoken_content = structured['text'].strip()
                if structured.get('response_type') in {'action_success', 'answer', 'clarification', 'error'}:
                    response_type = structured['response_type']
        except (ValueError, TypeError):
            pass
        # Keep complete pairs, bounded in RAM; never persist conversational transcripts.
        self.history = [*self.history, {'role': 'user', 'content': text},
                        {'role': 'assistant', 'content': content.strip()}][-24:]
        self.last_turn = time.monotonic()
        return Reply(spoken_content, self.name, response_type != 'error', response_type)


class HomeAssistantBackend:
    def __init__(self, name, spec):
        self.name, self.spec = name, spec
        self.url, self.token, self.timeout = settings(spec)
        self.reset()

    def reset(self):
        self.conversation_id = None
        self.last_turn = 0

    def respond(self, text):
        if time.monotonic() - self.last_turn > self.spec.get('session_idle_seconds', 300):
            self.reset()
        payload = {'text': text, 'language': self.spec.get('language', 'en'),
                   'agent_id': self.spec.get('agent_id', 'home_assistant')}
        if self.conversation_id:
            payload['conversation_id'] = self.conversation_id
        result = post_json(self.url + '/api/conversation/process', self.token, payload, self.timeout)
        try:
            response = result['response']
            speech = response.get('speech', {})
            content = speech.get('plain', speech.get('ssml', {})).get('speech')
            if not isinstance(content, str) or not content.strip():
                raise ValueError()
        except (KeyError, TypeError, AttributeError, ValueError):
            raise BackendError('Home Assistant returned no usable spoken answer.') from None
        self.conversation_id = result.get('conversation_id')
        self.last_turn = time.monotonic()
        kind = response.get('response_type')
        data = response.get('data') or {}
        failed = not isinstance(data, dict) or bool(data.get('failed'))
        response_type = ('error' if kind == 'error' or failed else
                         'clarification' if result.get('continue_conversation') else
                         'action_success' if kind == 'action_done' else
                         'answer' if kind == 'query_answer' else 'unknown')
        return Reply(html.unescape(re.sub('<[^>]*>', '', content)), self.name,
                     response_type != 'error', response_type)
