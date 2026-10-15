"""Conversation adapters. Voice/audio code depends only on ConversationBackend."""
from dataclasses import dataclass
from typing import Protocol


class BackendError(RuntimeError):
    """Safe-to-speak backend failure, without credentials or raw server responses."""


@dataclass(frozen=True)
class Reply:
    text: str
    backend: str
    success: bool = True
    response_type: str = 'unknown'


class ConversationBackend(Protocol):
    def respond(self, text: str) -> Reply: ...
    def reset(self) -> None: ...


def create_backend(name, specification):
    # Add a new protocol adapter here; OpenAI-compatible agents need config only.
    from .http import HomeAssistantBackend, OpenAIBackend
    adapters = {'openai': OpenAIBackend, 'homeassistant': HomeAssistantBackend}
    kind = specification.get('type')
    if kind not in adapters:
        raise ValueError(f'Unknown backend type: {kind}')
    return adapters[kind](name, specification)
