"""Plugin registry for dynamic plugin loading."""

import importlib
from typing import Dict, Type, Optional
from pathlib import Path
import yaml

from core.plugins.base import Plugin, PluginState


class PluginRegistry:
    """Registry for managing plugins."""
    
    def __init__(self):
        self._plugins: Dict[str, Plugin] = {}
        self._loaded = False
    
    def load_plugins(self, plugin_dir: Path = None):
        """Load all plugins from directory."""
        if self._loaded:
            return
        
        if plugin_dir is None:
            plugin_dir = Path(__file__).parent.parent.parent / "plugins"
        
        # Find all plugin directories
        for path in plugin_dir.iterdir():
            if path.is_dir() and (path / "manifest.yaml").exists():
                try:
                    self._load_plugin(path)
                except Exception as e:
                    print(f"Failed to load plugin {path.name}: {e}")
        
        self._loaded = True
    
    def _load_plugin(self, plugin_path: Path):
        """Load a single plugin."""
        # Load manifest
        with open(plugin_path / "manifest.yaml", 'r') as f:
            manifest = yaml.safe_load(f)
        
        plugin_name = manifest['name']
        
        # Import plugin module
        module_name = f"plugins.{plugin_name}"
        module = importlib.import_module(module_name)
        
        # Find plugin class
        plugin_class = None
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if (isinstance(attr, type) and 
                issubclass(attr, Plugin) and 
                attr != Plugin):
                plugin_class = attr
                break
        
        if not plugin_class:
            raise ValueError(f"No Plugin class found in {module_name}")
        
        # Create plugin instance
        plugin = plugin_class()
        self._plugins[plugin_name] = plugin
    
    def get_plugin(self, name: str) -> Optional[Plugin]:
        """Get a plugin by name."""
        if not self._loaded:
            self.load_plugins()
        return self._plugins.get(name)
    
    def get_state_function(
        self,
        plugin_name: str,
        state_name: str,
        config: Dict[str, Any]
    ):
        """Get a state function from a plugin."""
        plugin = self.get_plugin(plugin_name)
        if not plugin:
            raise ValueError(f"Plugin {plugin_name} not found")
        
        return plugin.get_state_function(state_name, config)
    
    def list_plugins(self) -> List[Dict[str, Any]]:
        """List all available plugins."""
        if not self._loaded:
            self.load_plugins()
        
        return [
            {
                "name": plugin.manifest.name,
                "version": plugin.manifest.version,
                "description": plugin.manifest.description,
                "states": list(plugin.manifest.states.keys())
            }
            for plugin in self._plugins.values()
        ]


# Global registry instance
plugin_registry = PluginRegistry()