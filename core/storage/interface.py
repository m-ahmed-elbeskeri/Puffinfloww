"""Storage backend interface."""

from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any
from datetime import datetime

from core.storage.events import WorkflowEvent
from core.agent.checkpoint import AgentCheckpoint


class StorageBackend(ABC):
    """Abstract base class for storage backends."""
    
    @abstractmethod
    async def initialize(self) -> None:
        """Initialize the storage backend."""
        pass
    
    @abstractmethod
    async def close(self) -> None:
        """Close the storage backend."""
        pass
    
    @abstractmethod
    async def save_event(self, event: WorkflowEvent) -> None:
        """Save a workflow event."""
        pass
    
    @abstractmethod
    async def load_events(
        self,
        workflow_id: str,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None
    ) -> List[WorkflowEvent]:
        """Load events for a workflow."""
        pass
    
    @abstractmethod
    async def save_checkpoint(
        self,
        workflow_id: str,
        checkpoint: AgentCheckpoint
    ) -> None:
        """Save an agent checkpoint."""
        pass
    
    @abstractmethod
    async def load_checkpoint(
        self,
        workflow_id: str
    ) -> Optional[AgentCheckpoint]:
        """Load the latest checkpoint for a workflow."""
        pass
    
    @abstractmethod
    async def list_workflows(
        self,
        limit: int = 100,
        offset: int = 0,
        filters: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """List workflows with optional filtering."""
        pass
    
    @abstractmethod
    async def delete_workflow(self, workflow_id: str) -> None:
        """Delete all data for a workflow."""
        pass