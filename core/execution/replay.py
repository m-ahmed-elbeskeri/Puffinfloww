"""Event-sourced replay engine for workflow execution."""

import asyncio
from typing import Dict, List, Optional, Any, Tuple, Set
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import hashlib
import json
import structlog
from copy import deepcopy

from core.agent.base import Agent
from core.agent.checkpoint import AgentCheckpoint
from core.storage.interface import StorageBackend
from core.storage.events import WorkflowEvent, EventType
from core.execution.determinism import DeterminismChecker, NonDeterministicError


logger = structlog.get_logger(__name__)


class ReplayMode(Enum):
    """Replay modes for workflow execution."""
    FULL = "full"  # Replay entire workflow from beginning
    PARTIAL = "partial"  # Replay from specific checkpoint
    VALIDATE = "validate"  # Validate execution matches events
    REPAIR = "repair"  # Attempt to repair diverged execution


@dataclass
class ReplayPoint:
    """Point in workflow execution for replay."""
    event_index: int
    timestamp: datetime
    state_name: Optional[str] = None
    checkpoint: Optional[AgentCheckpoint] = None
    event_hash: Optional[str] = None


@dataclass
class ReplayResult:
    """Result of replay operation."""
    success: bool
    mode: ReplayMode
    events_replayed: int
    divergence_point: Optional[ReplayPoint] = None
    errors: List[str] = field(default_factory=list)
    repaired_states: List[str] = field(default_factory=list)
    execution_hash: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "success": self.success,
            "mode": self.mode.value,
            "events_replayed": self.events_replayed,
            "divergence_point": {
                "event_index": self.divergence_point.event_index,
                "timestamp": self.divergence_point.timestamp.isoformat(),
                "state_name": self.divergence_point.state_name
            } if self.divergence_point else None,
            "errors": self.errors,
            "repaired_states": self.repaired_states,
            "execution_hash": self.execution_hash
        }


class ReplayValidator:
    """Validates replay execution against recorded events."""
    
    def __init__(self, determinism_checker: DeterminismChecker):
        self.determinism_checker = determinism_checker
        self._event_sequence: List[WorkflowEvent] = []
        self._execution_sequence: List[Dict[str, Any]] = []
    
    def record_event(self, event: WorkflowEvent) -> None:
        """Record an event during replay."""
        self._event_sequence.append(event)
    
    def record_execution(self, state_name: str, result: Any) -> None:
        """Record execution result during replay."""
        self._execution_sequence.append({
            "state_name": state_name,
            "result": result,
            "timestamp": datetime.utcnow()
        })
    
    def validate_sequence(self) -> Tuple[bool, Optional[int]]:
        """
        Validate execution sequence matches events.
        
        Returns:
            Tuple of (is_valid, divergence_index)
        """
        # Compare execution sequence with events
        execution_index = 0
        
        for event_index, event in enumerate(self._event_sequence):
            if event.event_type == EventType.STATE_STARTED:
                if execution_index >= len(self._execution_sequence):
                    return False, event_index
                
                exec_data = self._execution_sequence[execution_index]
                if exec_data["state_name"] != event.data.get("state_name"):
                    return False, event_index
                
                execution_index += 1
        
        return True, None
    
    def get_execution_hash(self) -> str:
        """Get hash of execution sequence."""
        sequence_str = json.dumps(
            [
                {
                    "state": e["state_name"],
                    "result": str(e["result"])
                }
                for e in self._execution_sequence
            ],
            sort_keys=True
        )
        return hashlib.sha256(sequence_str.encode()).hexdigest()


class ReplayEngine:
    """Engine for replaying workflow executions."""
    
    def __init__(
        self,
        storage: StorageBackend,
        determinism_checker: Optional[DeterminismChecker] = None
    ):
        self.storage = storage
        self.determinism_checker = determinism_checker or DeterminismChecker()
        self._replay_handlers: Dict[EventType, callable] = {}
        self._setup_handlers()
    
    def _setup_handlers(self):
        """Setup event replay handlers."""
        self._replay_handlers = {
            EventType.WORKFLOW_CREATED: self._handle_workflow_created,
            EventType.WORKFLOW_STARTED: self._handle_workflow_started,
            EventType.STATE_STARTED: self._handle_state_started,
            EventType.STATE_COMPLETED: self._handle_state_completed,
            EventType.STATE_FAILED: self._handle_state_failed,
            EventType.STATE_RETRIED: self._handle_state_retried,
            EventType.RESOURCE_ACQUIRED: self._handle_resource_acquired,
            EventType.RESOURCE_RELEASED: self._handle_resource_released,
            EventType.CHECKPOINT_SAVED: self._handle_checkpoint_saved,
        }
    
    async def replay(
        self,
        workflow_id: str,
        agent: Agent,
        mode: ReplayMode = ReplayMode.FULL,
        start_point: Optional[ReplayPoint] = None,
        end_point: Optional[ReplayPoint] = None
    ) -> ReplayResult:
        """
        Replay workflow execution.
        
        Args:
            workflow_id: ID of workflow to replay
            agent: Agent to replay with
            mode: Replay mode
            start_point: Optional starting point
            end_point: Optional ending point
            
        Returns:
            ReplayResult with outcome
        """
        logger.info(
            "replay_started",
            workflow_id=workflow_id,
            mode=mode.value,
            has_start_point=start_point is not None
        )
        
        # Load events
        events = await self.storage.load_events(workflow_id)
        if not events:
            return ReplayResult(
                success=False,
                mode=mode,
                events_replayed=0,
                errors=["No events found for workflow"]
            )
        
        # Filter events based on start/end points
        if start_point:
            events = events[start_point.event_index:]
        if end_point:
            events = events[:end_point.event_index + 1]
        
        # Execute replay based on mode
        if mode == ReplayMode.FULL:
            result = await self._replay_full(workflow_id, agent, events)
        elif mode == ReplayMode.PARTIAL:
            result = await self._replay_partial(workflow_id, agent, events, start_point)
        elif mode == ReplayMode.VALIDATE:
            result = await self._replay_validate(workflow_id, agent, events)
        elif mode == ReplayMode.REPAIR:
            result = await self._replay_repair(workflow_id, agent, events)
        else:
            result = ReplayResult(
                success=False,
                mode=mode,
                events_replayed=0,
                errors=[f"Unknown replay mode: {mode}"]
            )
        
        logger.info(
            "replay_completed",
            workflow_id=workflow_id,
            success=result.success,
            events_replayed=result.events_replayed
        )
        
        return result
    
    async def _replay_full(
        self,
        workflow_id: str,
        agent: Agent,
        events: List[WorkflowEvent]
    ) -> ReplayResult:
        """Full replay from beginning."""
        validator = ReplayValidator(self.determinism_checker)
        events_replayed = 0
        errors = []
        
        # Reset agent to initial state
        agent.completed_states.clear()
        agent.completed_once.clear()
        agent._running_states.clear()
        agent.shared_state.clear()
        
        try:
            # Replay each event
            for event in events:
                try:
                    await self._replay_event(agent, event, validator)
                    events_replayed += 1
                except Exception as e:
                    errors.append(f"Failed to replay {event.event_type.value}: {str(e)}")
                    
                    return ReplayResult(
                        success=False,
                        mode=ReplayMode.FULL,
                        events_replayed=events_replayed,
                        divergence_point=ReplayPoint(
                            event_index=events_replayed,
                            timestamp=event.timestamp,
                            state_name=event.data.get("state_name")
                        ),
                        errors=errors
                    )
            
            # Validate determinism
            is_valid, divergence_index = validator.validate_sequence()
            
            return ReplayResult(
                success=is_valid,
                mode=ReplayMode.FULL,
                events_replayed=events_replayed,
                divergence_point=ReplayPoint(
                    event_index=divergence_index,
                    timestamp=events[divergence_index].timestamp
                ) if divergence_index else None,
                execution_hash=validator.get_execution_hash()
            )
            
        except Exception as e:
            logger.error(
                "replay_full_error",
                workflow_id=workflow_id,
                error=str(e)
            )
            errors.append(str(e))
            
            return ReplayResult(
                success=False,
                mode=ReplayMode.FULL,
                events_replayed=events_replayed,
                errors=errors
            )
    
    async def _replay_partial(
        self,
        workflow_id: str,
        agent: Agent,
        events: List[WorkflowEvent],
        start_point: Optional[ReplayPoint]
    ) -> ReplayResult:
        """Partial replay from checkpoint."""
        if not start_point or not start_point.checkpoint:
            return ReplayResult(
                success=False,
                mode=ReplayMode.PARTIAL,
                events_replayed=0,
                errors=["No checkpoint provided for partial replay"]
            )
        
        # Restore from checkpoint
        await agent.restore_from_checkpoint(start_point.checkpoint)
        
        # Continue with full replay from this point
        return await self._replay_full(workflow_id, agent, events)
    
    async def _replay_validate(
        self,
        workflow_id: str,
        agent: Agent,
        events: List[WorkflowEvent]
    ) -> ReplayResult:
        """Validate replay matches recorded events."""
        # Create shadow copy of agent
        shadow_agent = deepcopy(agent)
        
        # Replay on shadow agent
        result = await self._replay_full(workflow_id, shadow_agent, events)
        
        # Compare with original execution
        if result.success:
            # Check determinism
            try:
                self.determinism_checker.validate_agent_state(shadow_agent)
            except NonDeterministicError as e:
                result.success = False
                result.errors.append(str(e))
        
        return result
    
    async def _replay_repair(
        self,
        workflow_id: str,
        agent: Agent,
        events: List[WorkflowEvent]
    ) -> ReplayResult:
        """Attempt to repair diverged execution."""
        validator = ReplayValidator(self.determinism_checker)
        events_replayed = 0
        repaired_states = []
        errors = []
        
        # First, try normal replay
        for i, event in enumerate(events):
            try:
                await self._replay_event(agent, event, validator)
                events_replayed += 1
            except Exception as e:
                # Attempt repair
                logger.warning(
                    "replay_repair_attempting",
                    event_index=i,
                    event_type=event.event_type.value,
                    error=str(e)
                )
                
                if event.event_type == EventType.STATE_FAILED:
                    # Try to recover failed state
                    state_name = event.data.get("state_name")
                    if state_name and state_name in agent.states:
                        try:
                            # Reset state metadata
                            if state_name in agent.state_metadata:
                                agent.state_metadata[state_name].attempts = 0
                                agent.state_metadata[state_name].status = "pending"
                            
                            # Re-run state
                            await agent.run_state(state_name)
                            repaired_states.append(state_name)
                            events_replayed += 1
                            
                            logger.info(
                                "replay_repair_success",
                                state_name=state_name
                            )
                            
                        except Exception as repair_error:
                            errors.append(
                                f"Failed to repair state {state_name}: {str(repair_error)}"
                            )
                else:
                    errors.append(f"Cannot repair {event.event_type.value}: {str(e)}")
        
        return ReplayResult(
            success=len(errors) == 0,
            mode=ReplayMode.REPAIR,
            events_replayed=events_replayed,
            errors=errors,
            repaired_states=repaired_states,
            execution_hash=validator.get_execution_hash()
        )
    
    async def _replay_event(
        self,
        agent: Agent,
        event: WorkflowEvent,
        validator: ReplayValidator
    ) -> None:
        """Replay a single event."""
        handler = self._replay_handlers.get(event.event_type)
        if handler:
            await handler(agent, event, validator)
        
        validator.record_event(event)
    
    # Event handlers
    async def _handle_workflow_created(
        self,
        agent: Agent,
        event: WorkflowEvent,
        validator: ReplayValidator
    ) -> None:
        """Handle workflow created event."""
        # Initialize agent state
        agent.name = event.data.get("agent_name", agent.name)
    
    async def _handle_workflow_started(
        self,
        agent: Agent,
        event: WorkflowEvent,
        validator: ReplayValidator
    ) -> None:
        """Handle workflow started event."""
        agent._session_start = event.timestamp.timestamp()
        agent.status = "running"
    
    async def _handle_state_started(
        self,
        agent: Agent,
        event: WorkflowEvent,
        validator: ReplayValidator
    ) -> None:
        """Handle state started event."""
        state_name = event.data.get("state_name")
        if state_name:
            agent._running_states.add(state_name)
            
            # Check determinism
            self.determinism_checker.check_state_start(
                state_name,
                event.data.get("inputs", {})
            )
    
    async def _handle_state_completed(
        self,
        agent: Agent,
        event: WorkflowEvent,
        validator: ReplayValidator
    ) -> None:
        """Handle state completed event."""
        state_name = event.data.get("state_name")
        if state_name:
            agent._running_states.discard(state_name)
            agent.completed_states.add(state_name)
            
            # Record execution
            validator.record_execution(
                state_name,
                event.data.get("result")
            )
            
            # Check determinism
            self.determinism_checker.check_state_completion(
                state_name,
                event.data.get("outputs", {})
            )
    
    async def _handle_state_failed(
        self,
        agent: Agent,
        event: WorkflowEvent,
        validator: ReplayValidator
    ) -> None:
        """Handle state failed event."""
        state_name = event.data.get("state_name")
        if state_name:
            agent._running_states.discard(state_name)
            
            # Update state metadata
            if state_name in agent.state_metadata:
                agent.state_metadata[state_name].status = "failed"
    
    async def _handle_state_retried(
        self,
        agent: Agent,
        event: WorkflowEvent,
        validator: ReplayValidator
    ) -> None:
        """Handle state retried event."""
        state_name = event.data.get("state_name")
        if state_name and state_name in agent.state_metadata:
            agent.state_metadata[state_name].attempts += 1
    
    async def _handle_resource_acquired(
        self,
        agent: Agent,
        event: WorkflowEvent,
        validator: ReplayValidator
    ) -> None:
        """Handle resource acquired event."""
        # Update resource pool state
        resource_type = event.data.get("resource_type")
        amount = event.data.get("amount", 0)
        
        if resource_type and hasattr(agent.resource_pool, "available"):
            # This would need proper resource type handling
            pass
    
    async def _handle_resource_released(
        self,
        agent: Agent,
        event: WorkflowEvent,
        validator: ReplayValidator
    ) -> None:
        """Handle resource released event."""
        # Update resource pool state
        resource_type = event.data.get("resource_type")
        amount = event.data.get("amount", 0)
        
        if resource_type and hasattr(agent.resource_pool, "available"):
            # This would need proper resource type handling
            pass
    
    async def _handle_checkpoint_saved(
        self,
        agent: Agent,
        event: WorkflowEvent,
        validator: ReplayValidator
    ) -> None:
        """Handle checkpoint saved event."""
        # Could restore from checkpoint data if needed
        pass
    
    async def find_replay_points(
        self,
        workflow_id: str
    ) -> List[ReplayPoint]:
        """Find available replay points for a workflow."""
        events = await self.storage.load_events(workflow_id)
        replay_points = []
        
        for i, event in enumerate(events):
            # Checkpoints are natural replay points
            if event.event_type == EventType.CHECKPOINT_SAVED:
                checkpoint = await self.storage.load_checkpoint(workflow_id)
                replay_points.append(
                    ReplayPoint(
                        event_index=i,
                        timestamp=event.timestamp,
                        checkpoint=checkpoint,
                        event_hash=hashlib.sha256(
                            json.dumps(event.data, sort_keys=True).encode()
                        ).hexdigest()
                    )
                )
            
            # State completions are also good replay points
            elif event.event_type == EventType.STATE_COMPLETED:
                replay_points.append(
                    ReplayPoint(
                        event_index=i,
                        timestamp=event.timestamp,
                        state_name=event.data.get("state_name")
                    )
                )
        
        return replay_points
    
    async def compare_executions(
        self,
        workflow_id1: str,
        workflow_id2: str
    ) -> Dict[str, Any]:
        """Compare two workflow executions."""
        events1 = await self.storage.load_events(workflow_id1)
        events2 = await self.storage.load_events(workflow_id2)
        
        # Extract state sequences
        states1 = [
            e.data.get("state_name")
            for e in events1
            if e.event_type == EventType.STATE_COMPLETED
        ]
        
        states2 = [
            e.data.get("state_name")
            for e in events2
            if e.event_type == EventType.STATE_COMPLETED
        ]
        
        # Compare
        return {
            "workflow1": workflow_id1,
            "workflow2": workflow_id2,
            "states1": states1,
            "states2": states2,
            "matching": states1 == states2,
            "common_states": list(set(states1) & set(states2)),
            "unique_to_1": list(set(states1) - set(states2)),
            "unique_to_2": list(set(states2) - set(states1))
        }