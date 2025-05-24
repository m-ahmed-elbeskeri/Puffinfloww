"""Dependency management types."""

from enum import Enum
from dataclasses import dataclass
from typing import Optional, Callable, Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from core.agent.base import Agent


class DependencyType(Enum):
    """Types of dependencies between states."""
    REQUIRED = "required"  # Must complete before state can run
    OPTIONAL = "optional"  # Will wait if running, otherwise skips
    PARALLEL = "parallel"  # Can run in parallel with dependency
    SEQUENTIAL = "sequential"  # Must run after dependency completes
    CONDITIONAL = "conditional"  # Depends on condition function
    TIMEOUT = "timeout"  # Wait for max time then continue
    XOR = "xor"  # Only one dependency needs to be satisfied
    AND = "and"  # All dependencies must be satisfied
    OR = "or"  # At least one dependency must be satisfied


class DependencyLifecycle(Enum):
    """Lifecycle management for dependencies."""
    ONCE = "once"  # Dependency only needs to be satisfied once
    ALWAYS = "always"  # Dependency must be satisfied every time
    SESSION = "session"  # Dependency valid for current run() execution
    TEMPORARY = "temporary"  # Dependency expires after specified time
    PERIODIC = "periodic"  # Must be re-satisfied after specified interval


@dataclass
class DependencyConfig:
    """Configuration for state dependencies."""
    type: DependencyType
    lifecycle: DependencyLifecycle = DependencyLifecycle.ALWAYS
    condition: Optional[Callable[["Agent"], bool]] = None
    expiry: Optional[float] = None
    interval: Optional[float] = None
    timeout: Optional[float] = None
    retry_policy: Optional[Dict[str, Any]] = None