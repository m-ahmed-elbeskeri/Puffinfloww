"""Event definitions for workflow orchestration."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Dict, Any, Optional


class EventType(Enum):
    """Workflow event types."""
    # Workflow lifecycle
    WORKFLOW_CREATED = "workflow_created"
    WORKFLOW_STARTED = "workflow_started"
    WORKFLOW_COMPLETED = "workflow_completed"
    WORKFLOW_FAILED = "workflow_failed"
    WORKFLOW_CANCELLED = "workflow_cancelled"
    WORKFLOW_PAUSED = "workflow_paused"
    WORKFLOW_RESUMED = "workflow_resumed"
    
    # State lifecycle
    STATE_SCHEDULED = "state_scheduled"
    STATE_STARTED = "state_started"
    STATE_COMPLETED = "state_completed"
    STATE_FAILED = "state_failed"
    STATE_RETRIED = "state_retried"
    STATE_COMPENSATED = "state_compensated"
    
    # Resource events
    RESOURCE_ACQUIRED = "resource_acquired"
    RESOURCE_RELEASED = "resource_released"
    RESOURCE_PREEMPTED = "resource_preempted"
    
    # Dependency events
    DEPENDENCY_SATISFIED = "dependency_satisfied"
    DEPENDENCY_BLOCKED = "dependency_blocked"


@dataclass
class WorkflowEvent:
    """Workflow event data."""
    workflow_id: str
    event_type: EventType
    timestamp: datetime
    data: Dict[str, Any]
    state_name: Optional[str] = None
    event_id: Optional[str] = None
    
    def __post_init__(self):
        if not self.event_id:
            import uuid
            self.event_id = str(uuid.uuid4())