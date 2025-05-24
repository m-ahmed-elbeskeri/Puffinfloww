"""Webhook plugin for HTTP requests and webhooks."""

from pathlib import Path
from typing import Dict, Type

from core.plugins.base import Plugin, PluginState
from plugins.webhook.states import (
    HTTPRequestState,
    WebhookReceiverState,
    WebhookSenderState
)


class WebhookPlugin(Plugin):
    """Webhook integration plugin."""
    
    def __init__(self):
        manifest_path = Path(__file__).parent / "manifest.yaml"
        super().__init__(manifest_path)
    
    def register_states(self) -> Dict[str, Type[PluginState]]:
        """Register webhook states."""
        return {
            "http_request": HTTPRequestState,
            "webhook_receiver": WebhookReceiverState,
            "webhook_sender": WebhookSenderState
        }