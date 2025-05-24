"""OpenAI plugin for GPT models and embeddings."""

from pathlib import Path
from typing import Dict, Type

from core.plugins.base import Plugin, PluginState
from plugins.openai.states import (
    ChatCompletionState,
    EmbeddingsState,
    FunctionCallingState
)


class OpenAIPlugin(Plugin):
    """OpenAI integration plugin."""
    
    def __init__(self):
        manifest_path = Path(__file__).parent / "manifest.yaml"
        super().__init__(manifest_path)
    
    def register_states(self) -> Dict[str, Type[PluginState]]:
        """Register OpenAI states."""
        return {
            "chat_completion": ChatCompletionState,
            "embeddings": EmbeddingsState,
            "function_calling": FunctionCallingState
        }