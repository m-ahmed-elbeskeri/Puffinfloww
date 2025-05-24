"""Plugin system base classes."""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List, Type
from dataclasses import dataclass
import yaml
from pathlib import Path

from core.agent.context import Context
from core.agent.state import StateResult


@dataclass
class PluginManifest:
    """Plugin manifest data."""
    name: str
    version: str
    description: str
    author: str
    states: Dict[str, Dict[str, Any]]
    inputs: Dict[str, Any]
    outputs: Dict[str, Any]
    resources: Dict[str, Any]
    dependencies: List[str]


class PluginState(ABC):
    """Base class for plugin states."""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
    
    @abstractmethod
    async def execute(self, context: Context) -> StateResult:
        """Execute the plugin state."""
        pass
    
    def validate_inputs(self, context: Context) -> None:
        """Validate required inputs are present."""
        # Override in subclasses
        pass
    
    def validate_outputs(self, context: Context) -> None:
        """Validate outputs were set correctly."""
        # Override in subclasses
        pass


class Plugin(ABC):
    """Base class for plugins."""
    
    def __init__(self, manifest_path: Path):
        self.manifest_path = manifest_path
        self.manifest = self._load_manifest()
        self._states: Dict[str, Type[PluginState]] = {}
    
    def _load_manifest(self) -> PluginManifest:
        """Load plugin manifest from YAML file."""
        with open(self.manifest_path, 'r') as f:
            data = yaml.safe_load(f)
        
        return PluginManifest(
            name=data['name'],
            version=data['version'],
            description=data['description'],
            author=data['author'],
            states=data.get('states', {}),
            inputs=data.get('inputs', {}),
            outputs=data.get('outputs', {}),
            resources=data.get('resources', {}),
            dependencies=data.get('dependencies', [])
        )
    
    @abstractmethod
    def register_states(self) -> Dict[str, Type[PluginState]]:
        """Register all plugin states."""
        pass
    
    def get_state(self, state_name: str) -> Optional[Type[PluginState]]:
        """Get a specific state class."""
        if not self._states:
            self._states = self.register_states()
        return self._states.get(state_name)
    
    def get_state_function(self, state_name: str, config: Dict[str, Any]):
        """Get a state function for use with Agent.add_state()."""
        state_class = self.get_state(state_name)
        if not state_class:
            raise ValueError(f"State {state_name} not found in plugin {self.manifest.name}")
        
        async def state_function(context: Context) -> StateResult:
            state = state_class(config)
            state.validate_inputs(context)
            result = await state.execute(context)
            state.validate_outputs(context)
            return result
        
        return state_function