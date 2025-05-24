"""Checkpoint management for agents."""

from dataclasses import dataclass, field
from typing import Dict, Set, List, Any, Optional, TYPE_CHECKING
import time

if TYPE_CHECKING:
    from core.agent.base import Agent
    from core.agent.state import AgentStatus, StateMetadata, PrioritizedState


@dataclass
class AgentCheckpoint:
    """Checkpoint data for agent state."""
    timestamp: float
    agent_name: str
    agent_status: "AgentStatus"
    priority_queue: List["PrioritizedState"]
    state_metadata: Dict[str, "StateMetadata"]
    running_states: Set[str]
    completed_states: Set[str]
    completed_once: Set[str]
    shared_state: Dict[str, Any]
    session_start: Optional[float]

    @classmethod
    def create_from_agent(cls, agent: "Agent") -> "AgentCheckpoint":
        """Create checkpoint from agent instance."""
        from copy import deepcopy
        
        return cls(
            timestamp=time.time(),
            agent_name=agent.name,
            agent_status=agent.status,
            priority_queue=deepcopy(agent.priority_queue),
            state_metadata=deepcopy(agent.state_metadata),
            running_states=set(agent._running_states),
            completed_states=set(agent.completed_states),
            completed_once=set(agent.completed_once),
            shared_state=deepcopy(agent.shared_state),
            session_start=agent._session_start
        )