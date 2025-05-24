"""Worker runner implementation."""

import asyncio
import uuid
from typing import Optional, Dict, Any
import structlog
from datetime import datetime

from core.agent.base import Agent
from core.execution.engine import WorkflowEngine
from core.storage.backends.sqlite import SQLiteBackend
from core.config import get_settings


logger = structlog.get_logger(__name__)


class WorkerRunner:
    """Worker runner for executing workflow tasks."""
    
    def __init__(
        self,
        queue_name: str,
        worker_id: Optional[str] = None,
        concurrency: int = 10,
        timeout: float = 300.0
    ):
        self.queue_name = queue_name
        self.worker_id = worker_id or str(uuid.uuid4())
        self.concurrency = concurrency
        self.timeout = timeout
        self.settings = get_settings()
        
        self._running = False
        self._tasks: set[asyncio.Task] = set()
        self._engine: Optional[WorkflowEngine] = None
    
    async def run(self) -> None:
        """Run the worker."""
        self._running = True
        
        # Initialize engine
        storage = SQLiteBackend(self.settings.database_url)
        self._engine = WorkflowEngine(storage)
        await self._engine.start()
        
        # Start heartbeat
        heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        
        try:
            # Main work loop
            while self._running:
                # Get tasks from queue (placeholder - would use Redis/SQS/etc)
                tasks = await self._get_tasks()
                
                for task in tasks:
                    if len(self._tasks) >= self.concurrency:
                        # Wait for a slot to open up
                        done, pending = await asyncio.wait(
                            self._tasks,
                            return_when=asyncio.FIRST_COMPLETED
                        )
                        self._tasks = pending
                    
                    # Process task
                    task_coro = self._process_task(task)
                    task_obj = asyncio.create_task(task_coro)
                    self._tasks.add(task_obj)
                
                if not tasks:
                    # No tasks available, wait a bit
                    await asyncio.sleep(1)
            
            # Wait for remaining tasks
            if self._tasks:
                await asyncio.gather(*self._tasks)
        
        finally:
            heartbeat_task.cancel()
            await self._engine.stop()
    
    async def stop(self) -> None:
        """Stop the worker."""
        logger.info("worker_stopping", worker_id=self.worker_id)
        self._running = False
        
        # Cancel all running tasks
        for task in self._tasks:
            task.cancel()
        
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
    
    async def _heartbeat_loop(self) -> None:
        """Send periodic heartbeats."""
        while self._running:
            await self._send_heartbeat()
            await asyncio.sleep(30)  # Heartbeat every 30 seconds
    
    async def _send_heartbeat(self) -> None:
        """Send worker heartbeat."""
        # In production, this would update a worker registry
        logger.debug(
            "worker_heartbeat",
            worker_id=self.worker_id,
            queue=self.queue_name,
            active_tasks=len(self._tasks)
        )
    
    async def _get_tasks(self) -> List[Dict[str, Any]]:
        """Get tasks from queue."""
        # Placeholder - in production would use Redis, SQS, etc.
        return []
    
    async def _process_task(self, task: Dict[str, Any]) -> None:
        """Process a single task."""
        task_id = task.get("id", str(uuid.uuid4()))
        
        logger.info(
            "task_started",
            task_id=task_id,
            worker_id=self.worker_id,
            task_type=task.get("type")
        )
        
        try:
            # Execute task based on type
            if task["type"] == "execute_state":
                await self._execute_state_task(task)
            elif task["type"] == "execute_workflow":
                await self._execute_workflow_task(task)
            else:
                logger.error("unknown_task_type", task_type=task["type"])
            
            logger.info("task_completed", task_id=task_id)
            
        except Exception as e:
            logger.error(
                "task_failed",
                task_id=task_id,
                error=str(e),
                error_type=type(e).__name__
            )
            # In production, would handle retry logic here
    
    async def _execute_state_task(self, task: Dict[str, Any]) -> None:
        """Execute a state task."""
        # This would reconstruct the agent and execute the specific state
        pass
    
    async def _execute_workflow_task(self, task: Dict[str, Any]) -> None:
        """Execute a workflow task."""
        workflow_id = task["workflow_id"]
        agent_config = task["agent_config"]
        
        # Reconstruct agent from config
        agent = Agent(
            name=agent_config["name"],
            max_concurrent=agent_config.get("max_concurrent", self.concurrency)
        )
        
        # Add states from config
        for state_config in agent_config["states"]:
            # This would properly reconstruct the state function
            pass
        
        # Execute workflow
        await self._engine.execute_workflow(
            workflow_id,
            agent,
            timeout=task.get("timeout", self.timeout)
        )