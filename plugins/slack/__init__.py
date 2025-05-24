"""Slack plugin for messaging and workspace operations."""

from pathlib import Path
from typing import Dict, Type

from core.plugins.base import Plugin, PluginState
from plugins.slack.states import (
    SendMessageState,
    ReadMessagesState,
    UploadFileState,
    CreateChannelState
)


class SlackPlugin(Plugin):
    """Slack integration plugin."""
    
    def __init__(self):
        manifest_path = Path(__file__).parent / "manifest.yaml"
        super().__init__(manifest_path)
    
    def register_states(self) -> Dict[str, Type[PluginState]]:
        """Register Slack states."""
        return {
            "send_message": SendMessageState,
            "read_messages": ReadMessagesState,
            "upload_file": UploadFileState,
            "create_channel": CreateChannelState
        }