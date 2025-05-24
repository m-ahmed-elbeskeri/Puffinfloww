"""Pydantic models for REST API."""

from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field
from datetime import datetime

from core.resources.requirements import ResourceRequirements


class WorkflowCreate(BaseModel):
    """Create workflow request."""
    name: str
    agent_name: str
    max_concurrent: Optional[int] = None
    timeout: Optional[float] = None
    metadata: Optional[Dict[str, Any]] = None
    auto_start: bool = False


class WorkflowResponse(BaseModel):
    """Workflow response."""
    workflow_id: str
    name: str
    agent_name: str
    status: str


class StateAdd(BaseModel):
    """Add state to workflow request."""
    name: str
    type: str = "custom"
    dependencies: Optional[Dict[str, str]] = None
    resources: Optional[ResourceRequirements] = None
    max_retries: int = 3
    config: Dict[str, Any] = Field(default_factory=dict)
    transitions: Optional[List[str]] = None


class WorkflowStatus(BaseModel):
    """Workflow status response."""
    workflow_id: str
    is_running: bool
    latest_event: str
    latest_timestamp: str
    total_events: int
    agent_status: Optional[str] = None
    completed_states: Optional[int] = None
    running_states: Optional[int] = None


class WorkflowPause(BaseModel):
    """Pause workflow request."""
    save_checkpoint: bool = True


class WorkflowResume(BaseModel):
    """Resume workflow request."""
    from_checkpoint: bool = True