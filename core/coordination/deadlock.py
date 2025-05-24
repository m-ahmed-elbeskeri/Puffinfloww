"""Deadlock detection for workflow execution from paste-2.txt."""

from typing import Dict, Set, Optional, List, Any, Tuple
import asyncio
import weakref
import structlog
from dataclasses import dataclass, field
from datetime import datetime
import time
from collections import defaultdict
from enum import Enum


logger = structlog.get_logger(__name__)


class DeadlockError(Exception):
    """Raised when a deadlock is detected"""
    
    def __init__(self, cycle: List[str], message: str = "Deadlock detected"):
        self.cycle = cycle
        super().__init__(f"{message}: {' -> '.join(cycle)}")


@dataclass
class ResourceNode:
    """Node in resource wait graph"""
    resource_id: str
    resource_type: str
    holders: Set[str] = field(default_factory=set)
    waiters: Set[str] = field(default_factory=set)
    acquired_at: Optional[datetime] = None
    
    def is_free(self) -> bool:
        """Check if resource is free"""
        return len(self.holders) == 0


@dataclass
class ProcessNode:
    """Node representing a process/state in wait graph"""
    process_id: str
    process_name: str
    holding: Set[str] = field(default_factory=set)  # Resources held
    waiting_for: Set[str] = field(default_factory=set)  # Resources waiting for
    started_at: datetime = field(default_factory=datetime.utcnow)
    blocked_at: Optional[datetime] = None
    
    def is_blocked(self) -> bool:
        """Check if process is blocked"""
        return len(self.waiting_for) > 0


@dataclass
class CycleDetectionResult:
    """Result of cycle detection"""
    has_cycle: bool
    cycles: List[List[str]] = field(default_factory=list)
    detection_time: datetime = field(default_factory=datetime.utcnow)
    graph_size: int = 0
    
    def get_shortest_cycle(self) -> Optional[List[str]]:
        """Get the shortest detected cycle"""
        if not self.cycles:
            return None
        return min(self.cycles, key=len)


class DependencyGraph:
    """Graph for tracking dependencies and detecting cycles"""
    
    def __init__(self):
        self.nodes: Dict[str, Set[str]] = {}  # node -> dependencies
        self.reverse_edges: Dict[str, Set[str]] = {}  # dependency -> nodes
        self._lock = asyncio.Lock()
    
    async def add_dependency(self, node: str, depends_on: str):
        """Add a dependency edge"""
        async with self._lock:
            if node not in self.nodes:
                self.nodes[node] = set()
            if depends_on not in self.reverse_edges:
                self.reverse_edges[depends_on] = set()
            
            self.nodes[node].add(depends_on)
            self.reverse_edges[depends_on].add(node)
    
    async def remove_dependency(self, node: str, depends_on: str):
        """Remove a dependency edge"""
        async with self._lock:
            if node in self.nodes:
                self.nodes[node].discard(depends_on)
                if not self.nodes[node]:
                    del self.nodes[node]
            
            if depends_on in self.reverse_edges:
                self.reverse_edges[depends_on].discard(node)
                if not self.reverse_edges[depends_on]:
                    del self.reverse_edges[depends_on]
    
    async def remove_node(self, node: str):
        """Remove a node and all its edges"""
        async with self._lock:
            # Remove outgoing edges
            if node in self.nodes:
                for dep in self.nodes[node]:
                    if dep in self.reverse_edges:
                        self.reverse_edges[dep].discard(node)
                del self.nodes[node]
            
            # Remove incoming edges
            if node in self.reverse_edges:
                for dependent in self.reverse_edges[node]:
                    if dependent in self.nodes:
                        self.nodes[dependent].discard(node)
                del self.reverse_edges[node]
    
    def find_cycles(self) -> CycleDetectionResult:
        """Find all cycles in the graph using DFS"""
        cycles = []
        visited = set()
        rec_stack = set()
        path = []
        
        def dfs(node: str) -> bool:
            """DFS to detect cycles"""
            visited.add(node)
            rec_stack.add(node)
            path.append(node)
            
            # Check all dependencies
            for neighbor in self.nodes.get(node, []):
                if neighbor not in visited:
                    if dfs(neighbor):
                        return True
                elif neighbor in rec_stack:
                    # Found a cycle
                    cycle_start = path.index(neighbor)
                    cycle = path[cycle_start:] + [neighbor]
                    cycles.append(cycle)
                    return True
            
            path.pop()
            rec_stack.remove(node)
            return False
        
        # Check all nodes
        for node in list(self.nodes.keys()):
            if node not in visited:
                dfs(node)
        
        return CycleDetectionResult(
            has_cycle=len(cycles) > 0,
            cycles=cycles,
            graph_size=len(self.nodes)
        )
    
    def topological_sort(self) -> Optional[List[str]]:
        """Perform topological sort if no cycles exist"""
        # Check for cycles first
        if self.find_cycles().has_cycle:
            return None
        
        # Kahn's algorithm
        in_degree = defaultdict(int)
        
        # Calculate in-degrees
        for node in self.nodes:
            if node not in in_degree:
                in_degree[node] = 0
            for dep in self.nodes[node]:
                in_degree[dep] += 1
        
        # Find nodes with no incoming edges
        queue = [node for node, degree in in_degree.items() if degree == 0]
        result = []
        
        while queue:
            node = queue.pop(0)
            result.append(node)
            
            # Remove edges from this node
            for neighbor in self.nodes.get(node, []):
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)
        
        # Check if all nodes were processed
        if len(result) == len(self.nodes):
            return result
        else:
            return None  # Cycle exists


class ResourceWaitGraph:
    """Wait-for graph for resource-based deadlock detection"""
    
    def __init__(self):
        self.resources: Dict[str, ResourceNode] = {}
        self.processes: Dict[str, ProcessNode] = {}
        self._lock = asyncio.Lock()
    
    async def add_resource(self, resource_id: str, resource_type: str = "generic"):
        """Add a resource to the graph"""
        async with self._lock:
            if resource_id not in self.resources:
                self.resources[resource_id] = ResourceNode(
                    resource_id=resource_id,
                    resource_type=resource_type
                )
    
    async def add_process(self, process_id: str, process_name: str = ""):
        """Add a process to the graph"""
        async with self._lock:
            if process_id not in self.processes:
                self.processes[process_id] = ProcessNode(
                    process_id=process_id,
                    process_name=process_name or process_id
                )
    
    async def acquire_resource(self, process_id: str, resource_id: str) -> bool:
        """Process acquires a resource"""
        async with self._lock:
            if resource_id not in self.resources:
                await self.add_resource(resource_id)
            if process_id not in self.processes:
                await self.add_process(process_id)
            
            resource = self.resources[resource_id]
            process = self.processes[process_id]
            
            # Check if resource is available
            if resource.is_free() or process_id in resource.holders:
                resource.holders.add(process_id)
                resource.acquired_at = datetime.utcnow()
                process.holding.add(resource_id)
                process.waiting_for.discard(resource_id)
                
                # Clear blocked status if not waiting for anything
                if not process.waiting_for:
                    process.blocked_at = None
                
                return True
            else:
                # Process must wait
                resource.waiters.add(process_id)
                process.waiting_for.add(resource_id)
                if process.blocked_at is None:
                    process.blocked_at = datetime.utcnow()
                
                return False
    
    async def release_resource(self, process_id: str, resource_id: str):
        """Process releases a resource"""
        async with self._lock:
            if resource_id in self.resources and process_id in self.processes:
                resource = self.resources[resource_id]
                process = self.processes[process_id]
                
                resource.holders.discard(process_id)
                process.holding.discard(resource_id)
                
                # Check if any waiters can acquire
                if resource.is_free() and resource.waiters:
                    # Simple FIFO - could be enhanced with priorities
                    next_process = next(iter(resource.waiters))
                    resource.waiters.remove(next_process)
                    
                    # Recursive acquisition
                    await self.acquire_resource(next_process, resource_id)
    
    async def request_resource(self, process_id: str, resource_id: str):
        """Process requests a resource (may block)"""
        async with self._lock:
            if resource_id not in self.resources:
                await self.add_resource(resource_id)
            if process_id not in self.processes:
                await self.add_process(process_id)
            
            resource = self.resources[resource_id]
            process = self.processes[process_id]
            
            # Add to waiting set
            resource.waiters.add(process_id)
            process.waiting_for.add(resource_id)
            if process.blocked_at is None:
                process.blocked_at = datetime.utcnow()
    
    def detect_deadlock(self) -> CycleDetectionResult:
        """Detect deadlocks using cycle detection in wait-for graph"""
        # Build wait-for graph
        wait_graph = DependencyGraph()
        
        # Add edges: if P1 waits for resource held by P2, add edge P1 -> P2
        for resource in self.resources.values():
            for waiter in resource.waiters:
                for holder in resource.holders:
                    if waiter != holder:
                        # Synchronous operation in async context
                        # In production, might want to make this async
                        asyncio.create_task(
                            wait_graph.add_dependency(waiter, holder)
                        )
        
        # Find cycles
        return wait_graph.find_cycles()
    
    def get_blocked_processes(self) -> List[ProcessNode]:
        """Get all currently blocked processes"""
        return [
            proc for proc in self.processes.values()
            if proc.is_blocked()
        ]
    
    def get_resource_holders(self, resource_id: str) -> Set[str]:
        """Get processes holding a resource"""
        if resource_id in self.resources:
            return self.resources[resource_id].holders.copy()
        return set()
    
    def get_resource_waiters(self, resource_id: str) -> Set[str]:
        """Get processes waiting for a resource"""
        if resource_id in self.resources:
            return self.resources[resource_id].waiters.copy()
        return set()


class DeadlockDetector:
    """Enhanced deadlock detection and resolution from paste-2.txt"""

    def __init__(
            self,
            agent: Any,
            detection_interval: float = 1.0,
            max_cycles: int = 100
    ):
        self.agent = weakref.proxy(agent)
        self.detection_interval = detection_interval
        self.max_cycles = max_cycles
        self._dependency_graph = DependencyGraph()
        self._resource_graph = ResourceWaitGraph()
        self._lock = asyncio.Lock()
        self._detection_task: Optional[asyncio.Task] = None
        self._cycle_count = 0
        self._last_cycle: Optional[List[str]] = None
        self._detection_history: List[CycleDetectionResult] = []

    async def start(self):
        """Start deadlock detection"""
        if not self._detection_task:
            self._detection_task = asyncio.create_task(self._detect_deadlocks())
            if hasattr(self.agent, '_monitor'):
                self.agent._monitor.logger.info("deadlock_detection_started")

    async def stop(self):
        """Stop deadlock detection"""
        if self._detection_task:
            self._detection_task.cancel()
            try:
                await self._detection_task
            except asyncio.CancelledError:
                pass
            self._detection_task = None
            if hasattr(self.agent, '_monitor'):
                self.agent._monitor.logger.info("deadlock_detection_stopped")

    def get_status(self) -> Dict[str, Any]:
        """Get detector status"""
        return {
            "cycle_count": self._cycle_count,
            "last_cycle": self._last_cycle,
            "active": bool(self._detection_task),
            "graph_size": len(self._dependency_graph.nodes),
            "resource_count": len(self._resource_graph.resources),
            "process_count": len(self._resource_graph.processes),
            "blocked_processes": len(self._resource_graph.get_blocked_processes())
        }
    
    async def add_dependency(self, from_state: str, to_state: str):
        """Add a dependency between states"""
        await self._dependency_graph.add_dependency(from_state, to_state)
    
    async def remove_dependency(self, from_state: str, to_state: str):
        """Remove a dependency between states"""
        await self._dependency_graph.remove_dependency(from_state, to_state)
    
    async def acquire_resource(
        self, 
        process_id: str, 
        resource_id: str,
        process_name: Optional[str] = None
    ) -> bool:
        """Process attempts to acquire a resource"""
        if process_name:
            await self._resource_graph.add_process(process_id, process_name)
        
        success = await self._resource_graph.acquire_resource(process_id, resource_id)
        
        # Check for deadlock immediately after failed acquisition
        if not success:
            result = self._resource_graph.detect_deadlock()
            if result.has_cycle:
                self._handle_deadlock(result)
        
        return success
    
    async def release_resource(self, process_id: str, resource_id: str):
        """Process releases a resource"""
        await self._resource_graph.release_resource(process_id, resource_id)
    
    async def _detect_deadlocks(self):
        """Main deadlock detection loop"""
        while True:
            try:
                await asyncio.sleep(self.detection_interval)
                
                # Check state dependencies
                state_result = self._dependency_graph.find_cycles()
                if state_result.has_cycle:
                    self._handle_deadlock(state_result)
                
                # Check resource wait graph
                resource_result = self._resource_graph.detect_deadlock()
                if resource_result.has_cycle:
                    self._handle_deadlock(resource_result)
                
                # Keep detection history
                self._detection_history.append(state_result)
                if len(self._detection_history) > 100:
                    self._detection_history.pop(0)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(
                    "deadlock_detection_error",
                    error=str(e)
                )
    
    def _handle_deadlock(self, result: CycleDetectionResult):
        """Handle detected deadlock"""
        self._cycle_count += 1
        self._last_cycle = result.get_shortest_cycle()
        
        logger.error(
            "deadlock_detected",
            cycle_count=self._cycle_count,
            cycle=self._last_cycle,
            cycles=len(result.cycles)
        )
        
        # Notify agent monitor if available
        if hasattr(self.agent, '_monitor'):
            self.agent._monitor.logger.error(
                "deadlock_detected",
                cycle=self._last_cycle
            )
        
        # Could implement automatic resolution strategies here:
        # - Kill youngest process in cycle
        # - Roll back transactions
        # - Preempt resources
        # For now, just raise an exception
        if self._last_cycle:
            raise DeadlockError(self._last_cycle)
    
    def get_dependency_graph(self) -> Dict[str, Set[str]]:
        """Get current dependency graph"""
        return dict(self._dependency_graph.nodes)
    
    def get_wait_graph(self) -> Dict[str, Dict[str, Any]]:
        """Get current wait-for graph"""
        graph = {}
        
        for process_id, process in self._resource_graph.processes.items():
            graph[process_id] = {
                "name": process.process_name,
                "holding": list(process.holding),
                "waiting_for": list(process.waiting_for),
                "blocked": process.is_blocked(),
                "blocked_duration": (
                    (datetime.utcnow() - process.blocked_at).total_seconds()
                    if process.blocked_at else 0
                )
            }
        
        return graph
    
    def find_potential_deadlocks(self) -> List[Tuple[str, str]]:
        """Find potential deadlock situations before they occur"""
        potential = []
        
        # Check for circular wait conditions
        for p1_id, p1 in self._resource_graph.processes.items():
            for p2_id, p2 in self._resource_graph.processes.items():
                if p1_id == p2_id:
                    continue
                
                # Check if P1 holds what P2 wants and vice versa
                p1_holds_p2_wants = bool(p1.holding & p2.waiting_for)
                p2_holds_p1_wants = bool(p2.holding & p1.waiting_for)
                
                if p1_holds_p2_wants and p2_holds_p1_wants:
                    potential.append((p1_id, p2_id))
        
        return potential