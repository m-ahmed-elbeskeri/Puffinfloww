"""Resource requirements definitions."""

from dataclasses import dataclass
from enum import Flag, auto
from typing import Optional

from core.agent.state import Priority


class ResourceType(Flag):
    """Types of resources that can be managed."""
    NONE = 0
    CPU = auto()
    MEMORY = auto()
    IO = auto()
    NETWORK = auto()
    GPU = auto()
    ALL = CPU | MEMORY | IO | NETWORK | GPU


@dataclass
class ResourceRequirements:
    """Resource requirements for state execution."""
    cpu_units: float = 1.0
    memory_mb: float = 100.0
    io_weight: float = 1.0
    network_weight: float = 1.0
    gpu_units: float = 0.0
    priority: Priority = Priority.NORMAL
    timeout: Optional[float] = None
    resource_types: ResourceType = ResourceType.ALL