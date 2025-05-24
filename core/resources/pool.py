"""Resource pool implementation"""

import asyncio
from collections import defaultdict
from typing import Dict, Optional, Set, List, Tuple, Any
import contextlib
import time
from dataclasses import dataclass  


from core.resources.requirements import ResourceRequirements, ResourceType


class ResourceAllocationError(Exception):
    """Base class for resource allocation errors."""
    pass


class ResourceOverflowError(ResourceAllocationError):
    """Raised when resource allocation would exceed limits."""
    pass


class ResourceQuotaExceededError(ResourceAllocationError):
    """Raised when state/agent exceeds its resource quota."""
    pass


@dataclass
class ResourceUsageStats:
    """Statistics for resource usage."""
    peak_usage: float = 0.0
    current_usage: float = 0.0
    total_allocations: int = 0
    failed_allocations: int = 0
    last_allocation_time: Optional[float] = None
    total_wait_time: float = 0.0


class ResourcePool:
    """Advanced resource management system."""

    def __init__(
        self,
        total_cpu: float = 4.0,
        total_memory: float = 1024.0,
        total_io: float = 100.0,
        total_network: float = 100.0,
        total_gpu: float = 0.0,
        enable_preemption: bool = False,
        enable_quotas: bool = False
    ):
        # Resource limits
        self.resources = {
            ResourceType.CPU: total_cpu,
            ResourceType.MEMORY: total_memory,
            ResourceType.IO: total_io,
            ResourceType.NETWORK: total_network,
            ResourceType.GPU: total_gpu
        }

        # Available resources
        self.available = self.resources.copy()

        # Core synchronization
        self._global_lock = asyncio.Lock()
        self._allocation_events: Dict[str, asyncio.Event] = {}

        # Allocation tracking
        self._allocations: Dict[str, Dict[ResourceType, float]] = {}
        self._allocation_times: Dict[str, float] = {}
        self._waiting_states: Set[str] = set()

        # Usage statistics
        self._usage_stats: Dict[ResourceType, ResourceUsageStats] = {
            rt: ResourceUsageStats() for rt in ResourceType if rt != ResourceType.NONE
        }

        # Quotas (if enabled)
        self._enable_quotas = enable_quotas
        self._quotas: Dict[str, Dict[ResourceType, float]] = defaultdict(dict)

        # Preemption (if enabled)
        self._enable_preemption = enable_preemption
        self._preempted_states: Set[str] = set()

        # Historical tracking
        self._usage_history: List[Tuple[float, Dict[ResourceType, float]]] = []
        self._history_retention = 3600  # 1 hour

    async def set_quota(self, state_name: str, resource_type: ResourceType, limit: float) -> None:
        """Set resource quota for a state."""
        if not self._enable_quotas:
            raise RuntimeError("Quotas are not enabled")

        async with self._global_lock:
            self._quotas[state_name][resource_type] = limit

    def _check_quota(self, state_name: str, requirements: ResourceRequirements) -> bool:
        """Check if allocation would exceed quota."""
        if not self._enable_quotas:
            return True

        current_usage = self._allocations.get(state_name, {})

        for resource_type in ResourceType:
            if resource_type == ResourceType.NONE:
                continue

            if resource_type not in requirements.resource_types:
                continue

            quota = self._quotas.get(state_name, {}).get(resource_type)
            if quota is None:
                continue

            required = getattr(requirements, f"{resource_type.name.lower()}_units", 0.0)
            current = current_usage.get(resource_type, 0.0)

            if current + required > quota:
                return False

        return True

    async def acquire(
        self,
        state_name: str,
        requirements: ResourceRequirements,
        timeout: Optional[float] = None,
        allow_preemption: bool = False
    ) -> bool:
        """Acquire resources with advanced features."""
        start_time = time.time()
        self._waiting_states.add(state_name)

        try:
            async with asyncio.timeout(timeout) if timeout else contextlib.nullcontext():
                while True:
                    async with self._global_lock:
                        # Validate requirements
                        self._validate_requirements(requirements)

                        # Check quotas
                        if not self._check_quota(state_name, requirements):
                            raise ResourceQuotaExceededError(
                                f"Resource quota exceeded for state {state_name}"
                            )

                        # Try allocation
                        if self._can_allocate(requirements):
                            self._allocate(state_name, requirements)
                            self._update_stats(state_name, requirements, start_time)
                            return True

                        # Handle preemption
                        if (self._enable_preemption and allow_preemption and
                                self._try_preemption(state_name, requirements)):
                            continue

                    # Wait for resources to become available
                    if state_name not in self._allocation_events:
                        self._allocation_events[state_name] = asyncio.Event()
                    await self._allocation_events[state_name].wait()
                    self._allocation_events[state_name].clear()

        except asyncio.TimeoutError:
            self._usage_stats[ResourceType.CPU].failed_allocations += 1
            return False

        finally:
            self._waiting_states.discard(state_name)

    def _validate_requirements(self, requirements: ResourceRequirements) -> None:
        """Validate resource requirements."""
        for resource_type in ResourceType:
            if resource_type == ResourceType.NONE:
                continue

            if resource_type not in requirements.resource_types:
                continue

            required = getattr(requirements, f"{resource_type.name.lower()}_units", 0.0)

            if required < 0:
                raise ValueError(f"Negative resource requirement for {resource_type}")

            if required > self.resources[resource_type]:
                raise ResourceOverflowError(
                    f"Resource requirement exceeds total available {resource_type}"
                )

    def _can_allocate(self, requirements: ResourceRequirements) -> bool:
        """Check if resources can be allocated."""
        for resource_type in ResourceType:
            if resource_type == ResourceType.NONE:
                continue

            if resource_type not in requirements.resource_types:
                continue

            required = getattr(requirements, f"{resource_type.name.lower()}_units", 0.0)
            if self.available[resource_type] < required:
                return False
        return True

    def _allocate(self, state_name: str, requirements: ResourceRequirements) -> None:
        """Allocate resources to a state."""
        self._allocations[state_name] = {}
        self._allocation_times[state_name] = time.time()

        for resource_type in ResourceType:
            if resource_type == ResourceType.NONE:
                continue

            if resource_type not in requirements.resource_types:
                continue

            required = getattr(requirements, f"{resource_type.name.lower()}_units", 0.0)
            self.available[resource_type] -= required
            self._allocations[state_name][resource_type] = required

    def _try_preemption(self, state_name: str, requirements: ResourceRequirements) -> bool:
        """Attempt to preempt lower priority states."""
        if not self._enable_preemption:
            return False

        # Find potential states to preempt
        candidates = []
        for other_state, alloc in self._allocations.items():
            if other_state == state_name:
                continue

            # Check if preempting would free enough resources
            would_free = {rt: 0.0 for rt in ResourceType if rt != ResourceType.NONE}
            for rt, amount in alloc.items():
                would_free[rt] += amount

            could_satisfy = True
            for rt in requirements.resource_types:
                if rt == ResourceType.NONE:
                    continue

                required = getattr(requirements, f"{rt.name.lower()}_units", 0.0)
                if self.available[rt] + would_free[rt] < required:
                    could_satisfy = False
                    break

            if could_satisfy:
                candidates.append(other_state)

        if not candidates:
            return False

        # Preempt states
        for other_state in candidates:
            self._preempt_state(other_state)

        return True

    def _preempt_state(self, state_name: str) -> None:
        """Preempt a state and return its resources."""
        if state_name not in self._allocations:
            return

        self._preempted_states.add(state_name)
        for resource_type, amount in self._allocations[state_name].items():
            self.available[resource_type] += amount

        del self._allocations[state_name]
        del self._allocation_times[state_name]

    async def release(self, state_name: str) -> None:
        """Release resources held by a state."""
        async with self._global_lock:
            if state_name not in self._allocations:
                return

            # Return resources
            for resource_type, amount in self._allocations[state_name].items():
                self.available[resource_type] += amount

            # Clean up tracking
            del self._allocations[state_name]
            del self._allocation_times[state_name]

            # Notify waiting states
            for waiting_state in self._waiting_states:
                if waiting_state in self._allocation_events:
                    self._allocation_events[waiting_state].set()

    def _update_stats(self, state_name: str, requirements: ResourceRequirements, start_time: float) -> None:
        """Update usage statistics."""
        wait_time = time.time() - start_time

        for resource_type in ResourceType:
            if resource_type == ResourceType.NONE:
                continue

            if resource_type not in requirements.resource_types:
                continue

            stats = self._usage_stats[resource_type]
            current_usage = sum(
                alloc.get(resource_type, 0.0) for alloc in self._allocations.values()
            )

            stats.current_usage = current_usage
            stats.peak_usage = max(stats.peak_usage, current_usage)
            stats.total_allocations += 1
            stats.last_allocation_time = time.time()
            stats.total_wait_time += wait_time

        # Record historical data point
        self._usage_history.append((time.time(), {
            rt: self.available[rt] for rt in ResourceType if rt != ResourceType.NONE
        }))

        # Cleanup old history
        cutoff = time.time() - self._history_retention
        while self._usage_history and self._usage_history[0][0] < cutoff:
            self._usage_history.pop(0)

    def get_usage_stats(self) -> Dict[ResourceType, ResourceUsageStats]:
        """Get current usage statistics."""
        return self._usage_stats.copy()

    def get_state_allocations(self) -> Dict[str, Dict[ResourceType, float]]:
        """Get current resource allocations by state."""
        return self._allocations.copy()

    def get_waiting_states(self) -> Set[str]:
        """Get states waiting for resources."""
        return self._waiting_states.copy()

    def get_preempted_states(self) -> Set[str]:
        """Get states that were preempted."""
        return self._preempted_states.copy()