"""Resource allocation strategies."""

import asyncio
from typing import Dict, List, Optional, Tuple, Any, Protocol
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from abc import ABC, abstractmethod
import heapq
import structlog
from collections import defaultdict

from core.resources.requirements import ResourceType, ResourceRequirements
from core.resources.pool import ResourcePool
from core.resources.quotas import QuotaManager, QuotaScope


logger = structlog.get_logger(__name__)


class AllocationStrategy(Enum):
    """Resource allocation strategies."""
    FIRST_FIT = "first_fit"  # First available slot
    BEST_FIT = "best_fit"  # Minimizes waste
    WORST_FIT = "worst_fit"  # Maximizes remaining space
    PRIORITY = "priority"  # Based on priority
    FAIR_SHARE = "fair_share"  # Equal distribution
    ROUND_ROBIN = "round_robin"  # Rotate allocations
    WEIGHTED = "weighted"  # Weighted by importance


@dataclass
class AllocationRequest:
    """Request for resource allocation."""
    request_id: str
    requester_id: str
    requirements: ResourceRequirements
    priority: int = 0
    weight: float = 1.0
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)
    deadline: Optional[datetime] = None
    
    def __lt__(self, other):
        """For priority queue ordering."""
        # Higher priority value = higher priority
        return self.priority > other.priority


@dataclass
class AllocationResult:
    """Result of allocation attempt."""
    request_id: str
    success: bool
    allocated: Dict[ResourceType, float] = field(default_factory=dict)
    reason: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.utcnow)
    allocation_time: Optional[float] = None  # Time taken to allocate
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "request_id": self.request_id,
            "success": self.success,
            "allocated": {
                rt.name: amount for rt, amount in self.allocated.items()
            },
            "reason": self.reason,
            "timestamp": self.timestamp.isoformat(),
            "allocation_time": self.allocation_time
        }


class AllocationMetrics:
    """Tracks allocation metrics."""
    
    def __init__(self):
        self.total_requests = 0
        self.successful_allocations = 0
        self.failed_allocations = 0
        self.total_allocation_time = 0.0
        self.resource_utilization: Dict[ResourceType, float] = defaultdict(float)
        self.queue_lengths: List[int] = []
        self.wait_times: List[float] = []
    
    def record_allocation(self, result: AllocationResult, wait_time: float = 0.0):
        """Record allocation metrics."""
        self.total_requests += 1
        
        if result.success:
            self.successful_allocations += 1
            for rt, amount in result.allocated.items():
                self.resource_utilization[rt] += amount
        else:
            self.failed_allocations += 1
        
        if result.allocation_time:
            self.total_allocation_time += result.allocation_time
        
        if wait_time > 0:
            self.wait_times.append(wait_time)
    
    def get_stats(self) -> Dict[str, Any]:
        """Get allocation statistics."""
        success_rate = (
            self.successful_allocations / self.total_requests 
            if self.total_requests > 0 else 0
        )
        
        avg_allocation_time = (
            self.total_allocation_time / self.successful_allocations
            if self.successful_allocations > 0 else 0
        )
        
        avg_wait_time = (
            sum(self.wait_times) / len(self.wait_times)
            if self.wait_times else 0
        )
        
        return {
            "total_requests": self.total_requests,
            "successful_allocations": self.successful_allocations,
            "failed_allocations": self.failed_allocations,
            "success_rate": success_rate,
            "avg_allocation_time": avg_allocation_time,
            "avg_wait_time": avg_wait_time,
            "resource_utilization": dict(self.resource_utilization)
        }


class ResourceAllocator(ABC):
    """Abstract base class for resource allocators."""
    
    def __init__(self, resource_pool: ResourcePool):
        self.resource_pool = resource_pool
        self.metrics = AllocationMetrics()
        self._pending_requests: List[AllocationRequest] = []
    
    @abstractmethod
    async def allocate(self, request: AllocationRequest) -> AllocationResult:
        """Allocate resources for a request."""
        pass
    
    @abstractmethod
    def get_allocation_order(
        self, 
        requests: List[AllocationRequest]
    ) -> List[AllocationRequest]:
        """Determine order of allocation for multiple requests."""
        pass
    
    async def allocate_batch(
        self, 
        requests: List[AllocationRequest]
    ) -> List[AllocationResult]:
        """Allocate resources for multiple requests."""
        ordered_requests = self.get_allocation_order(requests)
        results = []
        
        for request in ordered_requests:
            result = await self.allocate(request)
            results.append(result)
            self.metrics.record_allocation(result)
        
        return results
    
    def can_allocate(self, requirements: ResourceRequirements) -> bool:
        """Check if allocation is possible with current resources."""
        for resource_type in ResourceType:
            if resource_type == ResourceType.NONE:
                continue
                
            if resource_type not in requirements.resource_types:
                continue
                
            required = getattr(requirements, f"{resource_type.name.lower()}_units", 0.0)
            available = self.resource_pool.available.get(resource_type, 0.0)
            
            if required > available:
                return False
        
        return True


class FirstFitAllocator(ResourceAllocator):
    """First-fit allocation strategy."""
    
    async def allocate(self, request: AllocationRequest) -> AllocationResult:
        """Allocate using first-fit strategy."""
        start_time = time.time()
        
        # Try to acquire resources
        success = await self.resource_pool.acquire(
            request.request_id,
            request.requirements,
            timeout=0  # Non-blocking
        )
        
        if success:
            allocated = {}
            for resource_type in ResourceType:
                if resource_type == ResourceType.NONE:
                    continue
                if resource_type in request.requirements.resource_types:
                    allocated[resource_type] = getattr(
                        request.requirements, 
                        f"{resource_type.name.lower()}_units", 
                        0.0
                    )
            
            return AllocationResult(
                request_id=request.request_id,
                success=True,
                allocated=allocated,
                allocation_time=time.time() - start_time
            )
        else:
            return AllocationResult(
                request_id=request.request_id,
                success=False,
                reason="Insufficient resources",
                allocation_time=time.time() - start_time
            )
    
    def get_allocation_order(
        self, 
        requests: List[AllocationRequest]
    ) -> List[AllocationRequest]:
        """Order by arrival time (FIFO)."""
        return sorted(requests, key=lambda r: r.timestamp)


class BestFitAllocator(ResourceAllocator):
    """Best-fit allocation strategy - minimizes waste."""
    
    async def allocate(self, request: AllocationRequest) -> AllocationResult:
        """Allocate using best-fit strategy."""
        start_time = time.time()
        
        # Calculate waste for this allocation
        waste = self._calculate_waste(request.requirements)
        
        # Try to acquire resources
        success = await self.resource_pool.acquire(
            request.request_id,
            request.requirements,
            timeout=0
        )
        
        if success:
            allocated = {}
            for resource_type in ResourceType:
                if resource_type == ResourceType.NONE:
                    continue
                if resource_type in request.requirements.resource_types:
                    allocated[resource_type] = getattr(
                        request.requirements, 
                        f"{resource_type.name.lower()}_units", 
                        0.0
                    )
            
            logger.debug(
                "best_fit_allocation",
                request_id=request.request_id,
                waste=waste
            )
            
            return AllocationResult(
                request_id=request.request_id,
                success=True,
                allocated=allocated,
                allocation_time=time.time() - start_time
            )
        else:
            return AllocationResult(
                request_id=request.request_id,
                success=False,
                reason="Insufficient resources",
                allocation_time=time.time() - start_time
            )
    
    def _calculate_waste(self, requirements: ResourceRequirements) -> float:
        """Calculate resource waste for allocation."""
        total_waste = 0.0
        
        for resource_type in ResourceType:
            if resource_type == ResourceType.NONE:
                continue
                
            if resource_type not in requirements.resource_types:
                continue
                
            required = getattr(requirements, f"{resource_type.name.lower()}_units", 0.0)
            available = self.resource_pool.available.get(resource_type, 0.0)
            
            if available >= required:
                # Waste is remaining resources after allocation
                waste = available - required
                total_waste += waste
        
        return total_waste
    
    def get_allocation_order(
        self, 
        requests: List[AllocationRequest]
    ) -> List[AllocationRequest]:
        """Order by best fit (least waste)."""
        # Calculate waste for each request
        requests_with_waste = [
            (self._calculate_waste(req.requirements), req)
            for req in requests
        ]
        
        # Sort by waste (ascending)
        requests_with_waste.sort(key=lambda x: x[0])
        
        return [req for _, req in requests_with_waste]


class WorstFitAllocator(ResourceAllocator):
    """Worst-fit allocation strategy - maximizes remaining space."""
    
    async def allocate(self, request: AllocationRequest) -> AllocationResult:
        """Allocate using worst-fit strategy."""
        # Similar to best-fit but opposite ordering
        return await super().allocate(request)
    
    def get_allocation_order(
        self, 
        requests: List[AllocationRequest]
    ) -> List[AllocationRequest]:
        """Order by worst fit (most remaining space)."""
        # Similar to best-fit but sort descending
        requests_with_waste = [
            (self._calculate_remaining(req.requirements), req)
            for req in requests
        ]
        
        # Sort by remaining space (descending)
        requests_with_waste.sort(key=lambda x: x[0], reverse=True)
        
        return [req for _, req in requests_with_waste]
    
    def _calculate_remaining(self, requirements: ResourceRequirements) -> float:
        """Calculate remaining resources after allocation."""
        total_remaining = 0.0
        
        for resource_type in ResourceType:
            if resource_type == ResourceType.NONE:
                continue
                
            if resource_type not in requirements.resource_types:
                continue
                
            required = getattr(requirements, f"{resource_type.name.lower()}_units", 0.0)
            available = self.resource_pool.available.get(resource_type, 0.0)
            
            if available >= required:
                remaining = available - required
                total_remaining += remaining
        
        return total_remaining


class PriorityAllocator(ResourceAllocator):
    """Priority-based allocation strategy."""
    
    def __init__(self, resource_pool: ResourcePool):
        super().__init__(resource_pool)
        self._priority_queue: List[AllocationRequest] = []
    
    async def allocate(self, request: AllocationRequest) -> AllocationResult:
        """Allocate based on priority."""
        start_time = time.time()
        
        # Add to priority queue
        heapq.heappush(self._priority_queue, request)
        
        # Try to process queue
        processed = []
        while self._priority_queue:
            next_request = heapq.heappop(self._priority_queue)
            
            if self.can_allocate(next_request.requirements):
                success = await self.resource_pool.acquire(
                    next_request.request_id,
                    next_request.requirements,
                    timeout=0
                )
                
                if success:
                    processed.append(next_request)
                    
                    if next_request.request_id == request.request_id:
                        # This was our request
                        allocated = {}
                        for resource_type in ResourceType:
                            if resource_type == ResourceType.NONE:
                                continue
                            if resource_type in request.requirements.resource_types:
                                allocated[resource_type] = getattr(
                                    request.requirements, 
                                    f"{resource_type.name.lower()}_units", 
                                    0.0
                                )
                        
                        return AllocationResult(
                            request_id=request.request_id,
                            success=True,
                            allocated=allocated,
                            allocation_time=time.time() - start_time
                        )
                else:
                    # Put back in queue
                    heapq.heappush(self._priority_queue, next_request)
                    break
            else:
                # Can't allocate this or any lower priority
                heapq.heappush(self._priority_queue, next_request)
                break
        
        # Request not processed
        return AllocationResult(
            request_id=request.request_id,
            success=False,
            reason="Queued for resources",
            allocation_time=time.time() - start_time
        )
    
    def get_allocation_order(
        self, 
        requests: List[AllocationRequest]
    ) -> List[AllocationRequest]:
        """Order by priority."""
        return sorted(requests, key=lambda r: r.priority, reverse=True)


class FairShareAllocator(ResourceAllocator):
    """Fair-share allocation strategy."""
    
    def __init__(self, resource_pool: ResourcePool):
        super().__init__(resource_pool)
        self._usage_history: Dict[str, float] = defaultdict(float)
        self._allocation_counts: Dict[str, int] = defaultdict(int)
    
    async def allocate(self, request: AllocationRequest) -> AllocationResult:
        """Allocate with fair-share strategy."""
        start_time = time.time()
        
        # Calculate fair share for requester
        fair_share = self._calculate_fair_share(request.requester_id)
        
        # Check if within fair share
        current_usage = self._usage_history[request.requester_id]
        requested_total = self._calculate_resource_total(request.requirements)
        
        if current_usage + requested_total > fair_share:
            return AllocationResult(
                request_id=request.request_id,
                success=False,
                reason=f"Exceeds fair share (current: {current_usage}, "
                       f"limit: {fair_share})",
                allocation_time=time.time() - start_time
            )
        
        # Try to allocate
        success = await self.resource_pool.acquire(
            request.request_id,
            request.requirements,
            timeout=0
        )
        
        if success:
            # Update usage history
            self._usage_history[request.requester_id] += requested_total
            self._allocation_counts[request.requester_id] += 1
            
            allocated = {}
            for resource_type in ResourceType:
                if resource_type == ResourceType.NONE:
                    continue
                if resource_type in request.requirements.resource_types:
                    allocated[resource_type] = getattr(
                        request.requirements, 
                        f"{resource_type.name.lower()}_units", 
                        0.0
                    )
            
            return AllocationResult(
                request_id=request.request_id,
                success=True,
                allocated=allocated,
                allocation_time=time.time() - start_time
            )
        else:
            return AllocationResult(
                request_id=request.request_id,
                success=False,
                reason="Insufficient resources",
                allocation_time=time.time() - start_time
            )
    
    def _calculate_fair_share(self, requester_id: str) -> float:
        """Calculate fair share for a requester."""
        # Simple fair share: total resources / number of requesters
        total_requesters = len(self._usage_history) or 1
        total_resources = sum(self.resource_pool.resources.values())
        
        return total_resources / total_requesters
    
    def _calculate_resource_total(self, requirements: ResourceRequirements) -> float:
        """Calculate total resource units requested."""
        total = 0.0
        
        for resource_type in ResourceType:
            if resource_type == ResourceType.NONE:
                continue
                
            if resource_type in requirements.resource_types:
                total += getattr(
                    requirements, 
                    f"{resource_type.name.lower()}_units", 
                    0.0
                )
        
        return total
    
    def get_allocation_order(
        self, 
        requests: List[AllocationRequest]
    ) -> List[AllocationRequest]:
        """Order by usage history (least used first)."""
        return sorted(
            requests, 
            key=lambda r: self._usage_history[r.requester_id]
        )
    
    def reset_usage_history(self):
        """Reset usage history for new period."""
        self._usage_history.clear()
        self._allocation_counts.clear()


class WeightedAllocator(ResourceAllocator):
    """Weighted allocation strategy."""
    
    def __init__(self, resource_pool: ResourcePool):
        super().__init__(resource_pool)
        self._weights: Dict[str, float] = {}
    
    def set_weight(self, requester_id: str, weight: float):
        """Set weight for a requester."""
        self._weights[requester_id] = weight
    
    async def allocate(self, request: AllocationRequest) -> AllocationResult:
        """Allocate based on weights."""
        start_time = time.time()
        
        # Get weight for requester
        weight = self._weights.get(request.requester_id, request.weight)
        
        # Calculate weighted priority
        weighted_priority = request.priority * weight
        
        # Create weighted request
        weighted_request = AllocationRequest(
            request_id=request.request_id,
            requester_id=request.requester_id,
            requirements=request.requirements,
            priority=int(weighted_priority),
            weight=weight,
            metadata=request.metadata,
            timestamp=request.timestamp,
            deadline=request.deadline
        )
        
        # Use priority allocator with weighted priority
        priority_allocator = PriorityAllocator(self.resource_pool)
        return await priority_allocator.allocate(weighted_request)
    
    def get_allocation_order(
        self, 
        requests: List[AllocationRequest]
    ) -> List[AllocationRequest]:
        """Order by weighted priority."""
        weighted_requests = []
        
        for req in requests:
            weight = self._weights.get(req.requester_id, req.weight)
            weighted_priority = req.priority * weight
            weighted_requests.append((weighted_priority, req))
        
        # Sort by weighted priority (descending)
        weighted_requests.sort(key=lambda x: x[0], reverse=True)
        
        return [req for _, req in weighted_requests]


# Factory for creating allocators
def create_allocator(
    strategy: AllocationStrategy,
    resource_pool: ResourcePool
) -> ResourceAllocator:
    """Create an allocator based on strategy."""
    allocators = {
        AllocationStrategy.FIRST_FIT: FirstFitAllocator,
        AllocationStrategy.BEST_FIT: BestFitAllocator,
        AllocationStrategy.WORST_FIT: WorstFitAllocator,
        AllocationStrategy.PRIORITY: PriorityAllocator,
        AllocationStrategy.FAIR_SHARE: FairShareAllocator,
        AllocationStrategy.WEIGHTED: WeightedAllocator
    }
    
    allocator_class = allocators.get(strategy, FirstFitAllocator)
    return allocator_class(resource_pool)


import time