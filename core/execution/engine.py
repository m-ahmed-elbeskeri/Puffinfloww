"""Core workflow execution engine."""

import asyncio
import hashlib
import json
from typing import Dict, List, Optional, Any, Set
from datetime import datetime
import uuid
import structlog

from core.agent.base import Agent
from core.storage.interface import StorageBackend
from core.storage.events import WorkflowEvent, EventType
from core.monitoring.telemetry import TracingManager
from core.config import get_settings


logger = structlog.get_logger(__name__)


class WorkflowEngine:
    """Core workflow execution engine with event sourcing."""
    
    def __init__(
        self,
        storage: StorageBackend,
        tracing: Optional[TracingManager] = None
    ):
        self.storage = storage
        self.tracing = tracing or TracingManager()
        self.settings = get_settings()
        self._running_workflows: Dict[str, Agent] = {}
        self._event_handlers: Dict[EventType, List[callable]] = {
            event_type: [] for event_type in EventType
        }
    
    async def start(self) -> None:
        """Start the workflow engine."""
        await self.storage.initialize()
        logger.info("workflow_engine_started")
    
    async def stop(self) -> None:
        """Stop the workflow engine."""
        # Cancel all running workflows
        for workflow_id in list(self._running_workflows.keys()):
            await self.cancel_workflow(workflow_id)
        
        await self.storage.close()
        logger.info("workflow_engine_stopped")
    
    async def create_workflow(
        self,
        name: str,
        agent: Agent,
        metadata: Optional[Dict[str, Any]] = None
    ) -> str:
        """Create a new workflow instance."""
        workflow_id = str(uuid.uuid4())
        
        # Create initial event
        event = WorkflowEvent(
            workflow_id=workflow_id,
            event_type=EventType.WORKFLOW_CREATED,
            timestamp=datetime.utcnow(),
            data={
                "name": name,
                "agent_name": agent.name,
                "metadata": metadata or {}
            }
        )
        
        await self.storage.save_event(event)
        await self._emit_event(event)
        
        logger.info(
            "workflow_created",
            workflow_id=workflow_id,
            name=name,
            agent=agent.name
        )
        
        return workflow_id
    
    async def execute_workflow(
        self,
        workflow_id: str,
        agent: Agent,
        timeout: Optional[float] = None
    ) -> None:
        """Execute a workflow instance."""
        if workflow_id in self._running_workflows:
            raise RuntimeError(f"Workflow {workflow_id} is already running")
        
        self._running_workflows[workflow_id] = agent
        
        # Emit started event
        event = WorkflowEvent(
            workflow_id=workflow_id,
            event_type=EventType.WORKFLOW_STARTED,
            timestamp=datetime.utcnow(),
            data={"timeout": timeout}
        )
        await self.storage.save_event(event)
        await self._emit_event(event)
        
        try:
            with self.tracing.trace_workflow(workflow_id):
                await agent.run(timeout=timeout)
            
            # Emit completed event
            event = WorkflowEvent(
                workflow_id=workflow_id,
                event_type=EventType.WORKFLOW_COMPLETED,
                timestamp=datetime.utcnow(),
                data={
                    "completed_states": list(agent.completed_states),
                    "final_status": agent.status.value
                }
            )
            await self.storage.save_event(event)
            await self._emit_event(event)
            
        except Exception as e:
            # Emit failed event
            event = WorkflowEvent(
                workflow_id=workflow_id,
                event_type=EventType.WORKFLOW_FAILED,
                timestamp=datetime.utcnow(),
                data={
                    "error": str(e),
                    "error_type": type(e).__name__
                }
            )
            await self.storage.save_event(event)
            await self._emit_event(event)
            raise
            
        finally:
            self._running_workflows.pop(workflow_id, None)
    
    async def pause_workflow(self, workflow_id: str) -> Dict[str, Any]:
        """Pause a running workflow."""
        if workflow_id not in self._running_workflows:
            raise ValueError(f"Workflow {workflow_id} is not running")
        
        agent = self._running_workflows[workflow_id]
        checkpoint = await agent.pause()
        
        # Save checkpoint
        await self.storage.save_checkpoint(workflow_id, checkpoint)
        
        # Emit paused event
        event = WorkflowEvent(
            workflow_id=workflow_id,
            event_type=EventType.WORKFLOW_PAUSED,
            timestamp=datetime.utcnow(),
            data={"checkpoint_timestamp": checkpoint.timestamp}
        )
        await self.storage.save_event(event)
        await self._emit_event(event)
        
        return {
            "workflow_id": workflow_id,
            "status": "paused",
            "checkpoint": checkpoint.timestamp
        }
    
    async def resume_workflow(
        self,
        workflow_id: str,
        agent: Optional[Agent] = None
    ) -> None:
        """Resume a paused workflow."""
        # Load checkpoint
        checkpoint = await self.storage.load_checkpoint(workflow_id)
        if not checkpoint:
            raise ValueError(f"No checkpoint found for workflow {workflow_id}")
        
        # Use provided agent or create new one
        if not agent:
            # This would need to reconstruct the agent from checkpoint
            raise NotImplementedError("Agent reconstruction not implemented")
        
        await agent.restore_from_checkpoint(checkpoint)
        await agent.resume()
        
        # Emit resumed event
        event = WorkflowEvent(
            workflow_id=workflow_id,
            event_type=EventType.WORKFLOW_RESUMED,
            timestamp=datetime.utcnow(),
            data={"checkpoint_timestamp": checkpoint.timestamp}
        )
        await self.storage.save_event(event)
        await self._emit_event(event)
        
        # Continue execution
        await self.execute_workflow(workflow_id, agent)
    
    async def cancel_workflow(self, workflow_id: str) -> None:
        """Cancel a running workflow."""
        if workflow_id not in self._running_workflows:
            return
        
        agent = self._running_workflows[workflow_id]
        await agent.cancel_all()
        
        # Emit cancelled event
        event = WorkflowEvent(
            workflow_id=workflow_id,
            event_type=EventType.WORKFLOW_CANCELLED,
            timestamp=datetime.utcnow(),
            data={}
        )
        await self.storage.save_event(event)
        await self._emit_event(event)
        
        self._running_workflows.pop(workflow_id, None)
    
    async def get_workflow_status(self, workflow_id: str) -> Dict[str, Any]:
        """Get current status of a workflow."""
        events = await self.storage.load_events(workflow_id)
        if not events:
            raise ValueError(f"Workflow {workflow_id} not found")
        
        latest_event = events[-1]
        is_running = workflow_id in self._running_workflows
        
        status = {
            "workflow_id": workflow_id,
            "is_running": is_running,
            "latest_event": latest_event.event_type.value,
            "latest_timestamp": latest_event.timestamp.isoformat(),
            "total_events": len(events)
        }
        
        if is_running:
            agent = self._running_workflows[workflow_id]
            status.update({
                "agent_status": agent.status.value,
                "completed_states": len(agent.completed_states),
                "running_states": len(agent._running_states)
            })
        
        return status
    
    def on_event(self, event_type: EventType, handler: callable) -> None:
        """Register an event handler."""
        self._event_handlers[event_type].append(handler)
    
    async def _emit_event(self, event: WorkflowEvent) -> None:
        """Emit event to all registered handlers."""
        handlers = self._event_handlers.get(event.event_type, [])
        for handler in handlers:
            try:
                await handler(event)
            except Exception as e:
                logger.error(
                    "event_handler_error",
                    event_type=event.event_type.value,
                    error=str(e)
                )