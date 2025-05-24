"""Core Agent implementation from paste-4.txt."""

import asyncio
from collections import defaultdict
from enum import Enum, Flag, auto, IntEnum
from typing import (
    Callable, Dict, Optional, Union, List, Tuple, Set, Any,
    TypeVar, Generic, Protocol, runtime_checkable
)
from dataclasses import dataclass, field, asdict, replace
import contextlib
import heapq
import time
import uuid
from copy import deepcopy
import random

from core.agent.state import (
    Priority, AgentStatus, StateStatus, StateResult,
    StateFunction, StateMetadata, PrioritizedState
)
from core.agent.dependencies import (
    DependencyType, DependencyLifecycle, DependencyConfig
)
from core.agent.context import Context
from core.agent.checkpoint import AgentCheckpoint
from core.resources.requirements import ResourceRequirements
from core.resources.pool import ResourcePool


class RetryPolicy:
    """Configurable retry policy for state execution."""
    
    def __init__(
        self,
        max_retries: int = 3,
        initial_delay: float = 1.0,
        max_delay: float = 60.0,
        exponential_base: float = 2.0,
        jitter: bool = True
    ):
        self.max_retries = max_retries
        self.initial_delay = initial_delay
        self.max_delay = max_delay
        self.exponential_base = exponential_base
        self.jitter = jitter

    async def wait(self, attempt: int) -> None:
        if attempt >= self.max_retries:
            return

        delay = min(
            self.initial_delay * (self.exponential_base ** attempt),
            self.max_delay
        )

        if self.jitter:
            delay *= (0.5 + random.random())

        await asyncio.sleep(delay)


class Agent:
    """Advanced agent with scheduling, dependencies, resources, and checkpointing."""

    def __init__(
        self,
        name: str,
        max_concurrent: int = 10,
        state_timeout: Optional[float] = 60.0,
        resource_pool: Optional[ResourcePool] = None,
        retry_policy: Optional[RetryPolicy] = None
    ):
        self.name = name
        self.states: Dict[str, StateFunction] = {}
        self.state_metadata: Dict[str, StateMetadata] = {}
        self.priority_queue: List[PrioritizedState] = []
        self.shared_state: Dict[str, Any] = {}
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self.state_timeout = state_timeout
        self.resource_pool = resource_pool or ResourcePool()
        self.retry_policy = retry_policy or RetryPolicy()
        self._state_events: Dict[str, asyncio.Event] = {}
        self._running_states: Set[str] = set()
        self._session_start: Optional[float] = None
        self._cleanup_tasks: Set[asyncio.Task] = set()
        self._executed_states: Set[str] = set()

        # Checkpoint and pause support
        self.status = AgentStatus.IDLE
        self._pause_event = asyncio.Event()
        self._pause_event.set()  # Initially not paused
        self.completed_states: Set[str] = set()
        self.completed_once: Set[str] = set()

    def add_state(
        self,
        name: str,
        func: StateFunction,
        dependencies: Optional[Dict[str, Union[
            DependencyType,
            Tuple[DependencyType, DependencyLifecycle],
            Tuple[DependencyType, DependencyLifecycle, Callable],
            DependencyConfig
        ]]] = None,
        resources: Optional[ResourceRequirements] = None,
        max_retries: int = 3,
        retry_policy: Optional[RetryPolicy] = None
    ) -> None:
        """Add a state with enhanced configuration."""
        self.states[name] = func
        self._state_events[name] = asyncio.Event()

        metadata = StateMetadata(
            status=StateStatus.PENDING,
            max_retries=max_retries,
            resources=resources or ResourceRequirements()
        )

        if dependencies:
            for dep_name, dep_config in dependencies.items():
                if isinstance(dep_config, DependencyType):
                    metadata.dependencies[dep_name] = DependencyConfig(
                        type=dep_config
                    )
                elif isinstance(dep_config, tuple):
                    if len(dep_config) == 2:
                        dep_type, lifecycle = dep_config
                        metadata.dependencies[dep_name] = DependencyConfig(
                            type=dep_type,
                            lifecycle=lifecycle
                        )
                    elif len(dep_config) == 3:
                        dep_type, lifecycle, condition = dep_config
                        metadata.dependencies[dep_name] = DependencyConfig(
                            type=dep_type,
                            lifecycle=lifecycle,
                            condition=condition
                        )
                elif isinstance(dep_config, DependencyConfig):
                    metadata.dependencies[dep_name] = dep_config

        metadata.retry_policy = retry_policy or self.retry_policy
        self.state_metadata[name] = metadata

        if not dependencies:
            self._add_to_queue(name, metadata)

    def create_checkpoint(self) -> AgentCheckpoint:
        """Create a checkpoint of current agent state."""
        return AgentCheckpoint.create_from_agent(self)

    async def restore_from_checkpoint(self, checkpoint: AgentCheckpoint) -> None:
        """Restore agent state from checkpoint."""
        if checkpoint.agent_name != self.name:
            raise ValueError(f"Checkpoint is for agent '{checkpoint.agent_name}', not '{self.name}'")

        self.status = checkpoint.agent_status
        self.priority_queue = deepcopy(checkpoint.priority_queue)
        self.state_metadata = deepcopy(checkpoint.state_metadata)
        self._running_states = set(checkpoint.running_states)
        self.completed_states = set(checkpoint.completed_states)
        self.completed_once = set(checkpoint.completed_once)
        self.shared_state = deepcopy(checkpoint.shared_state)
        self._session_start = checkpoint.session_start

        # Recreate events
        self._state_events = {
            name: asyncio.Event() for name in self.states
        }
        for state in self.completed_states:
            self._state_events[state].set()

        # Set pause event based on status
        if self.status == AgentStatus.PAUSED:
            self._pause_event.clear()
        else:
            self._pause_event.set()

    async def pause(self) -> AgentCheckpoint:
        """Pause agent execution and return checkpoint."""
        if self.status == AgentStatus.RUNNING:
            self.status = AgentStatus.PAUSED
            self._pause_event.clear()
            return self.create_checkpoint()
        return None

    async def resume(self) -> None:
        """Resume agent execution."""
        if self.status == AgentStatus.PAUSED:
            self.status = AgentStatus.RUNNING
            self._pause_event.set()

    async def run(
        self,
        timeout: Optional[float] = None,
        cleanup_timeout: Optional[float] = 10.0
    ) -> None:
        """Run the agent with pause support and enhanced cleanup."""
        if not self._session_start:
            self._session_start = time.time()

        self.status = AgentStatus.RUNNING

        try:
            async with asyncio.timeout(timeout) if timeout else contextlib.nullcontext():
                while self.priority_queue or self._running_states:
                    # Check for pause
                    await self._pause_event.wait()

                    ready_states = await self._get_ready_states()

                    if not ready_states:
                        await asyncio.sleep(0.1)
                        continue

                    await asyncio.gather(*[
                        self.run_state(state) for state in ready_states
                    ])

            self.status = AgentStatus.COMPLETED

        except Exception as e:
            self.status = AgentStatus.FAILED
            raise

        finally:
            if self._cleanup_tasks:
                try:
                    async with asyncio.timeout(cleanup_timeout):
                        await asyncio.gather(*self._cleanup_tasks)
                except asyncio.TimeoutError:
                    pass

    async def _get_ready_states(self) -> List[str]:
        """Get states ready for execution."""
        ready_states = []

        while (self.priority_queue and
               len(ready_states) < self._semaphore._value):
            state = heapq.heappop(self.priority_queue)

            if await self._can_run(state.state_name):
                ready_states.append(state.state_name)
            else:
                self._add_to_queue(
                    state.state_name,
                    state.metadata,
                    priority_boost=-1
                )

        return ready_states

    def _add_to_queue(
        self,
        state_name: str,
        metadata: StateMetadata,
        priority_boost: int = 0
    ) -> None:
        """Add state to priority queue with optional boost."""
        if any(state.state_name == state_name for state in self.priority_queue):
            return  # Prevent duplicate additions

        heapq.heappush(
            self.priority_queue,
            PrioritizedState(
                -(metadata.resources.priority + priority_boost),
                time.time(),
                state_name,
                metadata
            )
        )

    async def run_state(self, state_name: str) -> None:
        """Run a state with pause support, error handling and resource management."""
        if state_name in self._executed_states:
            return

        metadata = self.state_metadata[state_name]
        metadata.status = StateStatus.RUNNING
        self._running_states.add(state_name)
        context = Context(self.shared_state)
        start_time = time.time()

        try:
            # Check for pause before resource acquisition
            await self._pause_event.wait()

            if not await self.resource_pool.acquire(
                state_name,
                metadata.resources,
                timeout=metadata.resources.timeout
            ):
                self._add_to_queue(state_name, metadata)
                return

            async with self._semaphore:
                while metadata.attempts < metadata.max_retries:
                    # Check for pause before each attempt
                    await self._pause_event.wait()
                    metadata.attempts += 1

                    try:
                        async with asyncio.timeout(
                            metadata.resources.timeout or self.state_timeout
                        ):
                            result = await self.states[state_name](context)

                        metadata.status = StateStatus.COMPLETED
                        metadata.last_execution = time.time()
                        metadata.last_success = time.time()
                        self._state_events[state_name].set()
                        self.completed_states.add(state_name)

                        await self._resolve_dependencies(state_name)

                        if result:
                            self._executed_states.add(state_name)
                            if isinstance(result, list):
                                transition_tasks = [
                                    self._handle_transition(step)
                                    for step in result
                                ]
                                await asyncio.gather(*transition_tasks)
                            else:
                                await self._handle_transition(result)
                        break

                    except asyncio.TimeoutError:
                        metadata.status = StateStatus.TIMEOUT
                        if metadata.attempts >= metadata.max_retries:
                            raise
                        await metadata.retry_policy.wait(metadata.attempts)

                    except Exception as e:
                        metadata.status = StateStatus.FAILED
                        if metadata.attempts >= metadata.max_retries:
                            raise
                        await metadata.retry_policy.wait(metadata.attempts)

        except Exception as e:
            metadata.status = StateStatus.FAILED
            cleanup_task = asyncio.create_task(
                self._handle_failure(state_name, e)
            )
            self._cleanup_tasks.add(cleanup_task)
            cleanup_task.add_done_callback(self._cleanup_tasks.discard)
            raise

        finally:
            self._executed_states.add(state_name)
            await self.resource_pool.release(state_name)
            self._running_states.discard(state_name)
            context.clear_state()

            if (metadata.status == StateStatus.COMPLETED and
                any(d.lifecycle == DependencyLifecycle.PERIODIC
                    for d in metadata.dependencies.values())):
                self._schedule_periodic_execution(state_name, metadata)

    async def _handle_failure(self, state_name: str, error: Exception) -> None:
        """Handle state execution failures."""
        metadata = self.state_metadata[state_name]

        # Reset dependent states that require this state
        for dep_name, dep_metadata in self.state_metadata.items():
            for dep in dep_metadata.dependencies.values():
                if (dep.type in {DependencyType.REQUIRED, DependencyType.SEQUENTIAL} and
                    state_name in dep_metadata.dependencies):
                    dep_metadata.status = StateStatus.PENDING
                    dep_metadata.satisfied_dependencies.discard(state_name)

        # Clear any cached results
        self._state_events[state_name].clear()
        self.completed_states.discard(state_name)

        # Add compensation task to queue if defined
        compensation_state = f"{state_name}_compensation"
        if compensation_state in self.states:
            self._add_to_queue(
                compensation_state,
                self.state_metadata[compensation_state],
                priority_boost=1
            )

    def _schedule_periodic_execution(
        self,
        state_name: str,
        metadata: StateMetadata
    ) -> None:
        """Schedule periodic re-execution of states."""
        min_interval = float('inf')

        # Find minimum interval from periodic dependencies
        for dep in metadata.dependencies.values():
            if (dep.lifecycle == DependencyLifecycle.PERIODIC and
                dep.interval is not None):
                min_interval = min(min_interval, dep.interval)

        if min_interval < float('inf'):
            # Schedule re-execution after interval
            self._add_to_queue(
                state_name,
                metadata,
                priority_boost=-int(min_interval)  # Lower priority for periodic tasks
            )

    async def _handle_transition(
        self,
        next_step: Union[str, Tuple["Agent", str]]
    ) -> None:
        """Handle state transitions with validation."""
        if isinstance(next_step, tuple):
            next_agent, next_state = next_step
            if not isinstance(next_agent, Agent) or not isinstance(next_state, str):
                raise ValueError(f"Invalid transition format: {next_step}")

            if next_state not in next_agent.states:
                raise ValueError(
                    f"Unknown state '{next_state}' in agent '{next_agent.name}'"
                )

            await next_agent.run_state(next_state)
        else:
            if next_step not in self.states:
                raise ValueError(f"Unknown state: {next_step}")

            self._add_to_queue(
                next_step,
                self.state_metadata[next_step]
            )

    async def _resolve_dependencies(self, state_name: str) -> None:
        """Resolve dependencies with lifecycle management."""
        current_time = time.time()

        for dependent_name, dependent_metadata in self.state_metadata.items():
            for dep_name, dep_config in dependent_metadata.dependencies.items():
                if dep_name != state_name:
                    continue

                # Handle different lifecycle types
                if dep_config.lifecycle == DependencyLifecycle.TEMPORARY:
                    if not dep_config.expiry:
                        dep_config.expiry = current_time + (dep_config.timeout or 3600)

                elif dep_config.lifecycle == DependencyLifecycle.PERIODIC:
                    if dep_config.interval:
                        dep_config.expiry = current_time + dep_config.interval

                # Mark dependency as satisfied if appropriate
                if dep_config.type in {
                    DependencyType.REQUIRED,
                    DependencyType.SEQUENTIAL
                }:
                    dependent_metadata.satisfied_dependencies.add(state_name)

                    # Add to queue if ready to run
                    if await self._can_run(dependent_name):
                        self._add_to_queue(
                            dependent_name,
                            dependent_metadata
                        )

    async def _can_run(self, state_name: str) -> bool:
        """Check if state can run with comprehensive dependency checking."""
        metadata = self.state_metadata[state_name]
        current_time = time.time()

        if metadata.status in {StateStatus.RUNNING, StateStatus.FAILED}:
            return False

        satisfied_groups: Dict[str, Set[str]] = {}

        for dep_name, dep_config in metadata.dependencies.items():
            dep_metadata = self.state_metadata.get(dep_name)
            if not dep_metadata:
                continue

            # Check if already satisfied based on lifecycle
            if dep_name in metadata.satisfied_dependencies:
                # Handle different lifecycle types
                if dep_config.lifecycle == DependencyLifecycle.ONCE:
                    continue

                elif dep_config.lifecycle == DependencyLifecycle.SESSION:
                    if (self._session_start and
                        dep_metadata.last_execution >= self._session_start):
                        continue

                elif dep_config.lifecycle == DependencyLifecycle.TEMPORARY:
                    if dep_config.expiry and current_time < dep_config.expiry:
                        continue

                elif dep_config.lifecycle == DependencyLifecycle.PERIODIC:
                    if (dep_config.interval and
                        dep_metadata.last_execution and
                        current_time < dep_metadata.last_execution + dep_config.interval):
                        continue

            # Group dependencies by type for complex dependency logic
            group_key = f"{dep_config.type}_{id(dep_config)}"
            if group_key not in satisfied_groups:
                satisfied_groups[group_key] = set()

            # Check dependency conditions
            is_satisfied = False

            if dep_config.type == DependencyType.REQUIRED:
                is_satisfied = dep_metadata.status == StateStatus.COMPLETED

            elif dep_config.type == DependencyType.OPTIONAL:
                is_satisfied = dep_name not in self._running_states

            elif dep_config.type == DependencyType.PARALLEL:
                is_satisfied = True  # Can always run in parallel

            elif dep_config.type == DependencyType.CONDITIONAL:
                is_satisfied = not dep_config.condition or dep_config.condition(self)

            if is_satisfied:
                satisfied_groups[group_key].add(dep_name)

        # Check complex dependency relationships
        for dep_name, dep_config in metadata.dependencies.items():
            group_key = f"{dep_config.type}_{id(dep_config)}"
            group = satisfied_groups[group_key]

            if dep_config.type == DependencyType.XOR:
                if len(group) != 1:
                    metadata.status = StateStatus.BLOCKED
                    return False

            elif dep_config.type == DependencyType.AND:
                if len(group) != len([
                    d for d in metadata.dependencies.values()
                    if f"{d.type}_{id(d)}" == group_key
                ]):
                    metadata.status = StateStatus.BLOCKED
                    return False

            elif dep_config.type == DependencyType.OR:
                if not group:
                    metadata.status = StateStatus.BLOCKED
                    return False

        metadata.status = StateStatus.READY
        return True

    def cancel_state(self, state_name: str) -> None:
        """Cancel a pending or running state."""
        if state_name in self.state_metadata:
            metadata = self.state_metadata[state_name]
            metadata.status = StateStatus.CANCELLED
            self._running_states.discard(state_name)
            self._state_events[state_name].set()

    async def cancel_all(self) -> None:
        """Cancel all pending and running states."""
        for state_name in list(self._running_states):
            self.cancel_state(state_name)

        self.priority_queue.clear()
        await asyncio.gather(*self._cleanup_tasks)