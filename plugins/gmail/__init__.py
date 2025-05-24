"""Gmail plugin for email operations."""

from pathlib import Path
from typing import Dict, Type

from core.plugins.base import Plugin, PluginState
from plugins.gmail.states import (
    SendEmailState,
    ReadEmailsState,
    SearchEmailsState
)


class GmailPlugin(Plugin):
    """Gmail integration plugin."""
    
    def __init__(self):
        manifest_path = Path(__file__).parent / "manifest.yaml"
        super().__init__(manifest_path)
    
    def register_states(self) -> Dict[str, Type[PluginState]]:
        """Register Gmail states."""
        return {
            "send_email": SendEmailState,
            "read_emails": ReadEmailsState,
            "search_emails": SearchEmailsState
        }