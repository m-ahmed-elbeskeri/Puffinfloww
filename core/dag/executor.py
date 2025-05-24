"""DAG execution engine."""

import asyncio
from typing import Dict, List, Set, Optional, Any, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import structlog
from collections import defaultdict

from core.dag.graph import DAG, DAGNode
from core.dag.parser import WorkflowDefinition, StateDefinition
from core.agent.base import Agent
from core.agent.context import Context


logger = structlog.get_logger(__name__)


class ExecutionStatus(Enum):
    """Execution status for DAG nodes."""
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


@dataclass
class NodeExecution:
    """Execution record for a DAG node."""
    node_id: str
    status: ExecutionStatus = ExecutionStatus.PENDING
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    result: Optional[Any] = None
    error: Optional[str] = None
    attempts: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionContext:
    """Context for DAG execution."""
    workflow_id: str
    execution_id: str
    dag: DAG
    shared_state: Dict[str, Any] = field(default_factory=dict)
    node_executions: Dict[str, NodeExecution] = field(default_factory=dict)
    start_time: datetime = field(default_factory=datetime.utcnow)
    end_time: Optional[datetime] = None
    
    def get_node_status(self, node_id: str) -> ExecutionStatus:
        """Get status of a node."""
        if node_id in self.node_executions:
            return self.node_executions[node_id].status
        return ExecutionStatus.PENDING
    
    def set_node_status(self, node_id: str, status: ExecutionStatus):
        """Set status of a node."""
        if node_id not in self.node_executions:
            self.node_executions[node_id] = NodeExecution(node_id=node_id)
        self.node_executions[node_id].status = status
    
    def is_complete(self) -> bool:
        """Check if execution is complete."""
        for node_id in self.dag._nodes:
            status = self.get_node_status(node_id)
            if status in (ExecutionStatus.PENDING, ExecutionStatus.READY, ExecutionStatus.RUNNING):
                return False
        return True
    
    def get_ready_nodes(self) -> List[str]:
        """Get nodes that are ready to execute."""
        ready = []
        
        for node_id in self.dag._nodes:
            if self.get_node_status(node_id) != ExecutionStatus.PENDING:
                continue
            
            # Check if all predecessors are complete
            predecessors_complete = True
            for pred in self.dag.get_predecessors(node_id):
                pred_status = self.get_node_status(pred)
                if pred_status not in (ExecutionStatus.SUCCESS, ExecutionStatus.SKIPPED):
                    predecessors_complete = False
                    break
            
            if predecessors_complete:
                ready.append(node_id)
        
        return ready


@dataclass
class ExecutionPlan:
    """Execution plan for DAG."""
    dag: DAG
    levels: List[List[str]]  # Nodes organized by execution level
    estimated_duration: Optional[float] = None
    parallelism: int = 10
    
    @classmethod
    def from_dag(cls, dag: DAG, parallelism: int = 10) -> "ExecutionPlan":
        """Create execution plan from DAG."""
        levels = dag.get_levels()
        return cls(dag=dag, levels=levels, parallelism=parallelism)
    
    def get_critical_path(self) -> List[str]:
        """Get critical path through DAG."""
        # Simple implementation - could be enhanced with actual duration estimates
        path = []
        
        # Start from nodes with no predecessors
        current_nodes = [
            node for node in self.dag._nodes
            if not self.dag.get_predecessors(node)
        ]
        
        while current_nodes:
            # Pick node with most descendants (heuristic)
            node = max(
                current_nodes,
                key=lambda n: len(self.dag.get_descendants(n))
            )
            path.append(node)
            
            # Move to successors
            current_nodes = list(self.dag.get_successors(node))
        
        return path


class DAGExecutor:
    """Executor for DAG-based workflows."""
    
    def __init__(
        self,
        max_concurrent: int = 10,
        node_timeout: float = 300.0
    ):
        self.max_concurrent = max_concurrent
        self.node_timeout = node_timeout
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._node_handlers: Dict[str, Callable] = {}
    
    def register_node_handler(
        self,
        node_type: str,
        handler: Callable[[str, Any, Context], Any]
    ):
        """Register handler for node type."""
        self._node_handlers[node_type] = handler
    
    async def execute(
        self,
        dag: DAG,
        context: Optional[ExecutionContext] = None
    ) -> ExecutionContext:
        """Execute a DAG."""
        if context is None:
            context = ExecutionContext(
                workflow_id=dag.name,
                execution_id=str(uuid.uuid4()),
                dag=dag
            )
        
        logger.info(
            "dag_execution_started",
            workflow_id=context.workflow_id,
            execution_id=context.execution_id,
            nodes=len(dag._nodes)
        )
        
        # Create execution plan
        plan = ExecutionPlan.from_dag(dag, self.max_concurrent)
        
        # Execute by levels
        for level_idx, level_nodes in enumerate(plan.levels):
            logger.debug(
                "executing_level",
                level=level_idx,
                nodes=level_nodes
            )
            
            # Execute nodes in level concurrently
            tasks = []
            for node_id in level_nodes:
                task = asyncio.create_task(
                    self._execute_node(node_id, context)
                )
                tasks.append(task)
            
            # Wait for level to complete
            await asyncio.gather(*tasks, return_exceptions=True)
            
            # Check for failures
            failed_nodes = [
                node_id for node_id in level_nodes
                if context.get_node_status(node_id) == ExecutionStatus.FAILED
            ]
            
            if failed_nodes:
                logger.error(
                    "level_execution_failed",
                    level=level_idx,
                    failed_nodes=failed_nodes
                )
                # Could implement failure handling strategy here
        
        context.end_time = datetime.utcnow()
        
        logger.info(
            "dag_execution_completed",
            workflow_id=context.workflow_id,
            execution_id=context.execution_id,
            duration=(context.end_time - context.start_time).total_seconds()
        )
        
        return context
    
    async def _execute_node(
        self,
        node_id: str,
        context: ExecutionContext
    ) -> None:
        """Execute a single node."""
        async with self._semaphore:
            node = context.dag.get_node(node_id)
            if not node:
                logger.error("node_not_found", node_id=node_id)
                return
            
            # Initialize execution record
            execution = NodeExecution(node_id=node_id)
            context.node_executions[node_id] = execution
            
            try:
                # Update status
                execution.status = ExecutionStatus.RUNNING
                execution.started_at = datetime.utcnow()
                execution.attempts += 1
                
                logger.info(
                    "node_execution_started",
                    node_id=node_id,
                    attempt=execution.attempts
                )
                
                # Execute node
                node_context = Context(context.shared_state)
                
                # Get handler based on node type
                handler = self._get_node_handler(node)
                
                # Execute with timeout
                result = await asyncio.wait_for(
                    handler(node_id, node.data, node_context),
                    timeout=self.node_timeout
                )
                
                # Update execution record
                execution.status = ExecutionStatus.SUCCESS
                execution.completed_at = datetime.utcnow()
                execution.result = result
                
                logger.info(
                    "node_execution_completed",
                    node_id=node_id,
                    duration=(execution.completed_at - execution.started_at).total_seconds()
                )
                
            except asyncio.TimeoutError:
                execution.status = ExecutionStatus.FAILED
                execution.completed_at = datetime.utcnow()
                execution.error = "Timeout"
                
                logger.error(
                    "node_execution_timeout",
                    node_id=node_id,
                    timeout=self.node_timeout
                )
                
            except Exception as e:
                execution.status = ExecutionStatus.FAILED
                execution.completed_at = datetime.utcnow()
                execution.error = str(e)
                
                logger.error(
                    "node_execution_failed",
                    node_id=node_id,
                    error=str(e),
                    attempt=execution.attempts
                )
    
    def _get_node_handler(self, node: DAGNode) -> Callable:
        """Get handler for node."""
        # Check registered handlers
        node_type = node.metadata.get("type", "default")
        if node_type in self._node_handlers:
            return self._node_handlers[node_type]
        
        # Default handler
        async def default_handler(node_id: str, data: Any, context: Context):
            logger.debug(
                "executing_default_handler",
                node_id=node_id,
                data_type=type(data).__name__
            )
            
            # If data is StateDefinition, execute as state
            if isinstance(data, StateDefinition):
                # Execute state logic
                context.set_state("current_state", node_id)
                context.set_state("state_config", data.config)
                
                # Simulate execution
                await asyncio.sleep(0.1)
                
                return {"status": "completed", "state": node_id}
            
            return {"status": "completed", "node": node_id}
        
        return default_handler


class DAGRuntime:
    """Runtime for DAG execution with agent integration."""
    
    def __init__(self):
        self.executor = DAGExecutor()
        self._agents: Dict[str, Agent] = {}
    
    async def execute_workflow(
        self,
        workflow: WorkflowDefinition,
        dag: DAG[StateDefinition]
    ) -> ExecutionContext:
        """Execute a workflow with its DAG."""
        # Create agent
        from core.dag.builder import DAGBuilder
        builder = DAGBuilder()
        agent = builder.build_agent(workflow, dag)
        
        # Store agent
        self._agents[workflow.name] = agent
        
        # Create execution context
        context = ExecutionContext(
            workflow_id=workflow.name,
            execution_id=str(uuid.uuid4()),
            dag=dag
        )
        
        # Register state handler
        async def state_handler(node_id: str, state_def: StateDefinition, ctx: Context):
            # Execute state through agent
            await agent.run_state(node_id)
            return {"state": node_id, "completed": True}
        
        self.executor.register_node_handler("task", state_handler)
        self.executor.register_node_handler("parallel", state_handler)
        
        # Execute DAG
        return await self.executor.execute(dag, context)
    
    def get_agent(self, workflow_name: str) -> Optional[Agent]:
        """Get agent for workflow."""
        return self._agents.get(workflow_name)


import uuid