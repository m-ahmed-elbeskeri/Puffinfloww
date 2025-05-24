"""Base scheduler implementation."""

import asyncio
from typing import Dict, List, Optional, Any, Callable, Union, Protocol
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
import uuid
import structlog
from abc import ABC, abstractmethod
import json
import heapq
from contextlib import asynccontextmanager

from core.agent.base import Agent
from core.execution.engine import WorkflowEngine


logger = structlog.get_logger(__name__)


class ScheduleType(Enum):
    """Types of schedules."""
    ONCE = "once"  # Run once at specific time
    INTERVAL = "interval"  # Run at regular intervals
    CRON = "cron"  # Cron expression
    EVENT = "event"  # Event-triggered
    MANUAL = "manual"  # Manual trigger only


class ScheduleStatus(Enum):
    """Schedule status."""
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Schedule:
    """Schedule configuration."""
    schedule_id: str
    name: str
    type: ScheduleType
    config: Dict[str, Any]
    workflow_id: Optional[str] = None
    agent_config: Optional[Dict[str, Any]] = None
    enabled: bool = True
    max_instances: int = 1  # Max concurrent instances
    timeout: Optional[float] = None
    retry_policy: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    
    # Runtime state
    status: ScheduleStatus = ScheduleStatus.ACTIVE
    last_run: Optional[datetime] = None
    next_run: Optional[datetime] = None
    run_count: int = 0
    error_count: int = 0
    active_instances: int = 0
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "schedule_id": self.schedule_id,
            "name": self.name,
            "type": self.type.value,
            "config": self.config,
            "workflow_id": self.workflow_id,
            "enabled": self.enabled,
            "status": self.status.value,
            "last_run": self.last_run.isoformat() if self.last_run else None,
            "next_run": self.next_run.isoformat() if self.next_run else None,
            "run_count": self.run_count,
            "error_count": self.error_count,
            "created_at": self.created_at.isoformat(),
            "metadata": self.metadata
        }


@dataclass
class ScheduledTask:
    """A scheduled task instance."""
    task_id: str
    schedule_id: str
    scheduled_time: datetime
    workflow_id: Optional[str] = None
    agent_config: Optional[Dict[str, Any]] = None
    status: str = "pending"
    attempts: int = 0
    result: Optional[Any] = None
    error: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    
    def __lt__(self, other):
        """For priority queue ordering."""
        return self.scheduled_time < other.scheduled_time


@dataclass
class TaskResult:
    """Result of task execution."""
    task_id: str
    schedule_id: str
    success: bool
    result: Optional[Any] = None
    error: Optional[str] = None
    duration: Optional[float] = None
    timestamp: datetime = field(default_factory=datetime.utcnow)


class ScheduleStore(Protocol):
    """Protocol for schedule persistence."""
    
    async def save_schedule(self, schedule: Schedule) -> None:
        """Save a schedule."""
        ...
    
    async def load_schedule(self, schedule_id: str) -> Optional[Schedule]:
        """Load a schedule by ID."""
        ...
    
    async def list_schedules(
        self,
        filters: Optional[Dict[str, Any]] = None
    ) -> List[Schedule]:
        """List all schedules."""
        ...
    
    async def delete_schedule(self, schedule_id: str) -> None:
        """Delete a schedule."""
        ...


class InMemoryScheduleStore:
    """In-memory schedule store for testing."""
    
    def __init__(self):
        self._schedules: Dict[str, Schedule] = {}
    
    async def save_schedule(self, schedule: Schedule) -> None:
        """Save a schedule."""
        self._schedules[schedule.schedule_id] = schedule
    
    async def load_schedule(self, schedule_id: str) -> Optional[Schedule]:
        """Load a schedule by ID."""
        return self._schedules.get(schedule_id)
    
    async def list_schedules(
        self,
        filters: Optional[Dict[str, Any]] = None
    ) -> List[Schedule]:
        """List all schedules."""
        schedules = list(self._schedules.values())
        
        if filters:
            # Apply filters
            if "enabled" in filters:
                schedules = [s for s in schedules if s.enabled == filters["enabled"]]
            if "type" in filters:
                schedules = [s for s in schedules if s.type == filters["type"]]
            if "status" in filters:
                schedules = [s for s in schedules if s.status == filters["status"]]
        
        return schedules
    
    async def delete_schedule(self, schedule_id: str) -> None:
        """Delete a schedule."""
        self._schedules.pop(schedule_id, None)


class Scheduler(ABC):
    """Base scheduler class."""
    
    def __init__(
        self,
        workflow_engine: Optional[WorkflowEngine] = None,
        store: Optional[ScheduleStore] = None
    ):
        self.workflow_engine = workflow_engine
        self.store = store or InMemoryScheduleStore()
        self._schedules: Dict[str, Schedule] = {}
        self._task_queue: List[ScheduledTask] = []
        self._running_tasks: Dict[str, ScheduledTask] = {}
        self._executor_task: Optional[asyncio.Task] = None
        self._scheduler_task: Optional[asyncio.Task] = None
        self._running = False
        self._lock = asyncio.Lock()
        
        # Callbacks
        self._task_callback: Optional[Callable[[ScheduledTask], Any]] = None
        self._error_callback: Optional[Callable[[str, Exception], None]] = None
    
    async def start(self) -> None:
        """Start the scheduler."""
        self._running = True
        
        # Load schedules from store
        schedules = await self.store.list_schedules()
        for schedule in schedules:
            self._schedules[schedule.schedule_id] = schedule
        
        # Start background tasks
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())
        self._executor_task = asyncio.create_task(self._executor_loop())
        
        logger.info("scheduler_started", schedules=len(self._schedules))
    
    async def stop(self) -> None:
        """Stop the scheduler."""
        self._running = False
        
        # Cancel background tasks
        if self._scheduler_task:
            self._scheduler_task.cancel()
            await asyncio.gather(self._scheduler_task, return_exceptions=True)
        
        if self._executor_task:
            self._executor_task.cancel()
            await asyncio.gather(self._executor_task, return_exceptions=True)
        
        # Save schedules
        for schedule in self._schedules.values():
            await self.store.save_schedule(schedule)
        
        logger.info("scheduler_stopped")
    
    async def add_schedule(self, schedule: Schedule) -> None:
        """Add a new schedule."""
        async with self._lock:
            self._schedules[schedule.schedule_id] = schedule
            await self.store.save_schedule(schedule)
            
            # Calculate next run
            next_run = await self._calculate_next_run(schedule)
            schedule.next_run = next_run
            
            logger.info(
                "schedule_added",
                schedule_id=schedule.schedule_id,
                name=schedule.name,
                type=schedule.type.value,
                next_run=next_run.isoformat() if next_run else None
            )
    
    async def remove_schedule(self, schedule_id: str) -> None:
        """Remove a schedule."""
        async with self._lock:
            if schedule_id in self._schedules:
                schedule = self._schedules.pop(schedule_id)
                schedule.status = ScheduleStatus.CANCELLED
                await self.store.delete_schedule(schedule_id)
                
                logger.info(
                    "schedule_removed",
                    schedule_id=schedule_id,
                    name=schedule.name
                )
    
    async def pause_schedule(self, schedule_id: str) -> None:
        """Pause a schedule."""
        async with self._lock:
            if schedule_id in self._schedules:
                schedule = self._schedules[schedule_id]
                schedule.status = ScheduleStatus.PAUSED
                schedule.enabled = False
                await self.store.save_schedule(schedule)
                
                logger.info(
                    "schedule_paused",
                    schedule_id=schedule_id,
                    name=schedule.name
                )
    
    async def resume_schedule(self, schedule_id: str) -> None:
        """Resume a paused schedule."""
        async with self._lock:
            if schedule_id in self._schedules:
                schedule = self._schedules[schedule_id]
                schedule.status = ScheduleStatus.ACTIVE
                schedule.enabled = True
                
                # Recalculate next run
                next_run = await self._calculate_next_run(schedule)
                schedule.next_run = next_run
                
                await self.store.save_schedule(schedule)
                
                logger.info(
                    "schedule_resumed",
                    schedule_id=schedule_id,
                    name=schedule.name,
                    next_run=next_run.isoformat() if next_run else None
                )
    
    async def trigger_schedule(self, schedule_id: str) -> str:
        """Manually trigger a schedule."""
        async with self._lock:
            if schedule_id not in self._schedules:
                raise ValueError(f"Schedule {schedule_id} not found")
            
            schedule = self._schedules[schedule_id]
            
            # Create task
            task = ScheduledTask(
                task_id=str(uuid.uuid4()),
                schedule_id=schedule_id,
                scheduled_time=datetime.utcnow(),
                workflow_id=schedule.workflow_id,
                agent_config=schedule.agent_config
            )
            
            heapq.heappush(self._task_queue, task)
            
            logger.info(
                "schedule_triggered",
                schedule_id=schedule_id,
                task_id=task.task_id
            )
            
            return task.task_id
    
    def set_task_callback(self, callback: Callable[[ScheduledTask], Any]) -> None:
        """Set callback for task execution."""
        self._task_callback = callback
    
    def set_error_callback(self, callback: Callable[[str, Exception], None]) -> None:
        """Set callback for errors."""
        self._error_callback = callback
    
    @abstractmethod
    async def _calculate_next_run(self, schedule: Schedule) -> Optional[datetime]:
        """Calculate next run time for schedule."""
        pass
    
    async def _scheduler_loop(self) -> None:
        """Main scheduler loop."""
        while self._running:
            try:
                current_time = datetime.utcnow()
                
                # Check all schedules
                async with self._lock:
                    for schedule in self._schedules.values():
                        if not schedule.enabled or schedule.status != ScheduleStatus.ACTIVE:
                            continue
                        
                        # Check if it's time to run
                        if schedule.next_run and current_time >= schedule.next_run:
                            # Check max instances
                            if schedule.active_instances < schedule.max_instances:
                                # Create task
                                task = ScheduledTask(
                                    task_id=str(uuid.uuid4()),
                                    schedule_id=schedule.schedule_id,
                                    scheduled_time=schedule.next_run,
                                    workflow_id=schedule.workflow_id,
                                    agent_config=schedule.agent_config
                                )
                                
                                heapq.heappush(self._task_queue, task)
                                schedule.active_instances += 1
                                
                                # Calculate next run
                                next_run = await self._calculate_next_run(schedule)
                                schedule.next_run = next_run
                                
                                logger.debug(
                                    "task_scheduled",
                                    schedule_id=schedule.schedule_id,
                                    task_id=task.task_id,
                                    next_run=next_run.isoformat() if next_run else None
                                )
                
                # Sleep for a short interval
                await asyncio.sleep(1)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("scheduler_loop_error", error=str(e))
                if self._error_callback:
                    self._error_callback("scheduler_loop", e)
    
    async def _executor_loop(self) -> None:
        """Task executor loop."""
        while self._running:
            try:
                # Get next task
                if self._task_queue:
                    current_time = datetime.utcnow()
                    
                    # Check if next task is ready
                    if self._task_queue[0].scheduled_time <= current_time:
                        task = heapq.heappop(self._task_queue)
                        
                        # Execute task
                        asyncio.create_task(self._execute_task(task))
                
                # Sleep briefly
                await asyncio.sleep(0.1)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("executor_loop_error", error=str(e))
                if self._error_callback:
                    self._error_callback("executor_loop", e)
    
    async def _execute_task(self, task: ScheduledTask) -> None:
        """Execute a scheduled task."""
        task.started_at = datetime.utcnow()
        task.status = "running"
        self._running_tasks[task.task_id] = task
        
        schedule = self._schedules.get(task.schedule_id)
        if not schedule:
            logger.error("schedule_not_found", task_id=task.task_id)
            return
        
        try:
            logger.info(
                "task_started",
                task_id=task.task_id,
                schedule_id=task.schedule_id,
                schedule_name=schedule.name
            )
            
            # Execute based on callback or workflow engine
            if self._task_callback:
                result = await self._task_callback(task)
            elif self.workflow_engine and task.workflow_id:
                # Create and execute workflow
                agent = self._create_agent_from_config(task.agent_config)
                await self.workflow_engine.execute_workflow(
                    task.workflow_id,
                    agent,
                    timeout=schedule.timeout
                )
                result = {"status": "completed"}
            else:
                raise ValueError("No execution method available")
            
            # Task completed successfully
            task.status = "completed"
            task.result = result
            task.completed_at = datetime.utcnow()
            
            # Update schedule
            schedule.last_run = task.started_at
            schedule.run_count += 1
            
            logger.info(
                "task_completed",
                task_id=task.task_id,
                duration=(task.completed_at - task.started_at).total_seconds()
            )
            
        except Exception as e:
            # Task failed
            task.status = "failed"
            task.error = str(e)
            task.completed_at = datetime.utcnow()
            task.attempts += 1
            
            # Update schedule
            schedule.error_count += 1
            
            logger.error(
                "task_failed",
                task_id=task.task_id,
                error=str(e),
                attempts=task.attempts
            )
            
            # Check retry policy
            if schedule.retry_policy and task.attempts < schedule.retry_policy.get("max_retries", 3):
                # Reschedule with backoff
                backoff = schedule.retry_policy.get("backoff", 60)
                task.scheduled_time = datetime.utcnow() + timedelta(seconds=backoff * task.attempts)
                task.status = "pending"
                heapq.heappush(self._task_queue, task)
                
                logger.info(
                    "task_retry_scheduled",
                    task_id=task.task_id,
                    attempt=task.attempts,
                    scheduled_time=task.scheduled_time.isoformat()
                )
            
            if self._error_callback:
                self._error_callback(f"task_{task.task_id}", e)
        
        finally:
            # Clean up
            self._running_tasks.pop(task.task_id, None)
            if schedule:
                schedule.active_instances = max(0, schedule.active_instances - 1)
    
    def _create_agent_from_config(self, config: Optional[Dict[str, Any]]) -> Agent:
        """Create agent from configuration."""
        if not config:
            raise ValueError("No agent configuration provided")
        
        # This is a placeholder - would need proper deserialization
        from core.agent import Agent
        agent = Agent(name=config.get("name", "scheduled_agent"))
        
        # Add states from config
        # ...
        
        return agent
    
    def get_schedule(self, schedule_id: str) -> Optional[Schedule]:
        """Get a schedule by ID."""
        return self._schedules.get(schedule_id)
    
    def list_schedules(self, **filters) -> List[Schedule]:
        """List schedules with optional filters."""
        schedules = list(self._schedules.values())
        
        # Apply filters
        if "enabled" in filters:
            schedules = [s for s in schedules if s.enabled == filters["enabled"]]
        if "type" in filters:
            schedules = [s for s in schedules if s.type == filters["type"]]
        if "status" in filters:
            schedules = [s for s in schedules if s.status == filters["status"]]
        
        return schedules
    
    def get_running_tasks(self) -> List[ScheduledTask]:
        """Get currently running tasks."""
        return list(self._running_tasks.values())
    
    def get_pending_tasks(self) -> List[ScheduledTask]:
        """Get pending tasks."""
        return sorted(self._task_queue)
    
    async def get_stats(self) -> Dict[str, Any]:
        """Get scheduler statistics."""
        return {
            "total_schedules": len(self._schedules),
            "active_schedules": len([s for s in self._schedules.values() if s.enabled]),
            "running_tasks": len(self._running_tasks),
            "pending_tasks": len(self._task_queue),
            "total_runs": sum(s.run_count for s in self._schedules.values()),
            "total_errors": sum(s.error_count for s in self._schedules.values())
        }


class IntervalScheduler(Scheduler):
    """Scheduler for interval-based schedules."""
    
    async def _calculate_next_run(self, schedule: Schedule) -> Optional[datetime]:
        """Calculate next run time for interval schedule."""
        if schedule.type != ScheduleType.INTERVAL:
            return None
        
        interval = schedule.config.get("interval", 60)  # seconds
        
        if schedule.last_run:
            return schedule.last_run + timedelta(seconds=interval)
        else:
            # First run
            start_time = schedule.config.get("start_time")
            if start_time:
                return datetime.fromisoformat(start_time)
            else:
                return datetime.utcnow()


class OnceScheduler(Scheduler):
    """Scheduler for one-time schedules."""
    
    async def _calculate_next_run(self, schedule: Schedule) -> Optional[datetime]:
        """Calculate next run time for once schedule."""
        if schedule.type != ScheduleType.ONCE:
            return None
        
        if schedule.run_count > 0:
            # Already ran
            schedule.status = ScheduleStatus.COMPLETED
            return None
        
        run_at = schedule.config.get("run_at")
        if run_at:
            return datetime.fromisoformat(run_at)
        else:
            return datetime.utcnow()