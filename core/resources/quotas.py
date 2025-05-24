"""Quota management for resource allocation."""

import asyncio
from typing import Dict, Optional, Set, Tuple, List, Any, Union
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
import structlog
from collections import defaultdict
import time

from core.resources.requirements import ResourceType


logger = structlog.get_logger(__name__)


class QuotaScope(Enum):
    """Scope of quota enforcement."""
    AGENT = "agent"  # Per-agent quotas
    WORKFLOW = "workflow"  # Per-workflow quotas
    STATE = "state"  # Per-state quotas
    USER = "user"  # Per-user quotas (for multi-tenancy)
    GLOBAL = "global"  # Global system quotas


class QuotaPolicy(Enum):
    """Quota enforcement policies."""
    HARD = "hard"  # Strict enforcement, reject if exceeds
    SOFT = "soft"  # Allow temporary exceed with warning
    BURST = "burst"  # Allow burst up to certain limit
    RATE_LIMIT = "rate_limit"  # Limit rate of resource usage


@dataclass
class QuotaLimit:
    """Definition of a quota limit."""
    resource_type: ResourceType
    limit: float
    scope: QuotaScope
    policy: QuotaPolicy = QuotaPolicy.HARD
    burst_limit: Optional[float] = None  # For burst policy
    rate_limit: Optional[float] = None  # Requests per second
    window_size: timedelta = field(default_factory=lambda: timedelta(minutes=1))
    cooldown: timedelta = field(default_factory=lambda: timedelta(minutes=5))
    
    def __post_init__(self):
        if self.policy == QuotaPolicy.BURST and self.burst_limit is None:
            self.burst_limit = self.limit * 1.5
        if self.policy == QuotaPolicy.RATE_LIMIT and self.rate_limit is None:
            self.rate_limit = self.limit


@dataclass
class QuotaUsage:
    """Track quota usage."""
    current: float = 0.0
    peak: float = 0.0
    total_allocated: float = 0.0
    total_released: float = 0.0
    allocations: int = 0
    violations: int = 0
    last_violation: Optional[datetime] = None
    last_reset: datetime = field(default_factory=datetime.utcnow)
    
    # For rate limiting
    request_times: List[float] = field(default_factory=list)
    
    def reset(self):
        """Reset usage statistics."""
        self.current = 0.0
        self.allocations = 0
        self.last_reset = datetime.utcnow()
        self.request_times.clear()
    
    def add_allocation(self, amount: float):
        """Record an allocation."""
        self.current += amount
        self.total_allocated += amount
        self.peak = max(self.peak, self.current)
        self.allocations += 1
        self.request_times.append(time.time())
    
    def remove_allocation(self, amount: float):
        """Record a release."""
        self.current = max(0, self.current - amount)
        self.total_released += amount
    
    def record_violation(self):
        """Record a quota violation."""
        self.violations += 1
        self.last_violation = datetime.utcnow()


@dataclass
class QuotaMetrics:
    """Metrics for quota usage."""
    scope: QuotaScope
    scope_id: str
    resource_type: ResourceType
    usage: QuotaUsage
    limit: QuotaLimit
    
    @property
    def utilization(self) -> float:
        """Get current utilization percentage."""
        if self.limit.limit == 0:
            return 0.0
        return (self.usage.current / self.limit.limit) * 100
    
    @property
    def is_exceeded(self) -> bool:
        """Check if quota is exceeded."""
        return self.usage.current > self.limit.limit
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "scope": self.scope.value,
            "scope_id": self.scope_id,
            "resource_type": self.resource_type.name,
            "current_usage": self.usage.current,
            "limit": self.limit.limit,
            "utilization": self.utilization,
            "peak_usage": self.usage.peak,
            "allocations": self.usage.allocations,
            "violations": self.usage.violations,
            "policy": self.limit.policy.value
        }


class QuotaExceededError(Exception):
    """Raised when quota is exceeded."""
    
    def __init__(self, scope: QuotaScope, scope_id: str, 
                 resource_type: ResourceType, requested: float, 
                 available: float):
        self.scope = scope
        self.scope_id = scope_id
        self.resource_type = resource_type
        self.requested = requested
        self.available = available
        super().__init__(
            f"Quota exceeded for {scope.value} '{scope_id}': "
            f"requested {requested} {resource_type.name}, "
            f"available {available}"
        )


class QuotaManager:
    """Manages resource quotas across different scopes."""
    
    def __init__(self):
        # Quota limits by scope
        self._limits: Dict[QuotaScope, Dict[str, Dict[ResourceType, QuotaLimit]]] = {
            scope: defaultdict(dict) for scope in QuotaScope
        }
        
        # Usage tracking
        self._usage: Dict[QuotaScope, Dict[str, Dict[ResourceType, QuotaUsage]]] = {
            scope: defaultdict(lambda: defaultdict(QuotaUsage)) for scope in QuotaScope
        }
        
        # Locks for thread safety
        self._locks: Dict[str, asyncio.Lock] = {}
        
        # Background tasks
        self._cleanup_task: Optional[asyncio.Task] = None
        self._running = False
    
    async def start(self):
        """Start the quota manager."""
        self._running = True
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("quota_manager_started")
    
    async def stop(self):
        """Stop the quota manager."""
        self._running = False
        if self._cleanup_task:
            self._cleanup_task.cancel()
            await asyncio.gather(self._cleanup_task, return_exceptions=True)
        logger.info("quota_manager_stopped")
    
    def set_quota(
        self,
        scope: QuotaScope,
        scope_id: str,
        resource_type: ResourceType,
        limit: Union[float, QuotaLimit]
    ):
        """Set a quota limit."""
        if isinstance(limit, (int, float)):
            quota_limit = QuotaLimit(
                resource_type=resource_type,
                limit=float(limit),
                scope=scope
            )
        else:
            quota_limit = limit
        
        self._limits[scope][scope_id][resource_type] = quota_limit
        
        logger.info(
            "quota_set",
            scope=scope.value,
            scope_id=scope_id,
            resource_type=resource_type.name,
            limit=quota_limit.limit,
            policy=quota_limit.policy.value
        )
    
    def remove_quota(
        self,
        scope: QuotaScope,
        scope_id: str,
        resource_type: Optional[ResourceType] = None
    ):
        """Remove a quota limit."""
        if resource_type:
            self._limits[scope][scope_id].pop(resource_type, None)
            self._usage[scope][scope_id].pop(resource_type, None)
        else:
            # Remove all quotas for this scope_id
            self._limits[scope].pop(scope_id, None)
            self._usage[scope].pop(scope_id, None)
    
    async def check_quota(
        self,
        scope: QuotaScope,
        scope_id: str,
        resource_type: ResourceType,
        requested: float
    ) -> bool:
        """
        Check if allocation would exceed quota.
        
        Returns:
            True if allocation is allowed, False otherwise
        """
        # Get lock for this scope_id
        lock_key = f"{scope.value}:{scope_id}"
        if lock_key not in self._locks:
            self._locks[lock_key] = asyncio.Lock()
        
        async with self._locks[lock_key]:
            # Check if quota exists
            if scope_id not in self._limits[scope]:
                return True  # No quota set
            
            if resource_type not in self._limits[scope][scope_id]:
                return True  # No quota for this resource
            
            limit = self._limits[scope][scope_id][resource_type]
            usage = self._usage[scope][scope_id][resource_type]
            
            # Check based on policy
            if limit.policy == QuotaPolicy.HARD:
                return usage.current + requested <= limit.limit
            
            elif limit.policy == QuotaPolicy.SOFT:
                # Allow but warn if exceeding
                if usage.current + requested > limit.limit:
                    logger.warning(
                        "soft_quota_exceeded",
                        scope=scope.value,
                        scope_id=scope_id,
                        resource_type=resource_type.name,
                        current=usage.current,
                        requested=requested,
                        limit=limit.limit
                    )
                return True
            
            elif limit.policy == QuotaPolicy.BURST:
                # Allow burst up to burst_limit
                return usage.current + requested <= limit.burst_limit
            
            elif limit.policy == QuotaPolicy.RATE_LIMIT:
                # Check rate limit
                return self._check_rate_limit(usage, limit)
            
            return False
    
    def _check_rate_limit(self, usage: QuotaUsage, limit: QuotaLimit) -> bool:
        """Check if rate limit is exceeded."""
        current_time = time.time()
        window_start = current_time - limit.window_size.total_seconds()
        
        # Remove old requests outside window
        usage.request_times = [
            t for t in usage.request_times if t > window_start
        ]
        
        # Check rate
        requests_in_window = len(usage.request_times)
        max_requests = limit.rate_limit * limit.window_size.total_seconds()
        
        return requests_in_window < max_requests
    
    async def allocate(
        self,
        scope: QuotaScope,
        scope_id: str,
        resource_type: ResourceType,
        amount: float
    ) -> bool:
        """
        Allocate resources against quota.
        
        Returns:
            True if allocation succeeded, False otherwise
        
        Raises:
            QuotaExceededError: If hard quota is exceeded
        """
        # Check quota
        if not await self.check_quota(scope, scope_id, resource_type, amount):
            # Get current usage for error message
            usage = self._usage[scope][scope_id][resource_type]
            limit = self._limits[scope][scope_id][resource_type]
            
            if limit.policy == QuotaPolicy.HARD:
                raise QuotaExceededError(
                    scope, scope_id, resource_type, 
                    amount, limit.limit - usage.current
                )
            else:
                usage.record_violation()
                return False
        
        # Record allocation
        lock_key = f"{scope.value}:{scope_id}"
        async with self._locks[lock_key]:
            usage = self._usage[scope][scope_id][resource_type]
            usage.add_allocation(amount)
        
        return True
    
    async def release(
        self,
        scope: QuotaScope,
        scope_id: str,
        resource_type: ResourceType,
        amount: float
    ):
        """Release allocated resources."""
        lock_key = f"{scope.value}:{scope_id}"
        if lock_key not in self._locks:
            return
        
        async with self._locks[lock_key]:
            if scope_id in self._usage[scope]:
                if resource_type in self._usage[scope][scope_id]:
                    usage = self._usage[scope][scope_id][resource_type]
                    usage.remove_allocation(amount)
    
    def get_usage(
        self,
        scope: QuotaScope,
        scope_id: str,
        resource_type: Optional[ResourceType] = None
    ) -> Union[QuotaUsage, Dict[ResourceType, QuotaUsage]]:
        """Get current usage for a scope."""
        if scope_id not in self._usage[scope]:
            return {} if resource_type is None else QuotaUsage()
        
        if resource_type:
            return self._usage[scope][scope_id].get(resource_type, QuotaUsage())
        else:
            return dict(self._usage[scope][scope_id])
    
    def get_metrics(
        self,
        scope: Optional[QuotaScope] = None,
        scope_id: Optional[str] = None
    ) -> List[QuotaMetrics]:
        """Get quota metrics."""
        metrics = []
        
        scopes = [scope] if scope else list(QuotaScope)
        
        for s in scopes:
            scope_ids = [scope_id] if scope_id else list(self._limits[s].keys())
            
            for sid in scope_ids:
                if sid not in self._limits[s]:
                    continue
                
                for resource_type, limit in self._limits[s][sid].items():
                    usage = self._usage[s][sid].get(resource_type, QuotaUsage())
                    
                    metrics.append(QuotaMetrics(
                        scope=s,
                        scope_id=sid,
                        resource_type=resource_type,
                        usage=usage,
                        limit=limit
                    ))
        
        return metrics
    
    def reset_usage(
        self,
        scope: QuotaScope,
        scope_id: str,
        resource_type: Optional[ResourceType] = None
    ):
        """Reset usage statistics."""
        if scope_id in self._usage[scope]:
            if resource_type:
                if resource_type in self._usage[scope][scope_id]:
                    self._usage[scope][scope_id][resource_type].reset()
            else:
                for usage in self._usage[scope][scope_id].values():
                    usage.reset()
    
    async def _cleanup_loop(self):
        """Periodic cleanup of old usage data."""
        while self._running:
            try:
                await asyncio.sleep(3600)  # Run hourly
                
                current_time = time.time()
                
                # Clean up old rate limit data
                for scope in self._usage.values():
                    for scope_id_usage in scope.values():
                        for usage in scope_id_usage.values():
                            # Keep only recent request times
                            cutoff = current_time - 3600  # Keep 1 hour
                            usage.request_times = [
                                t for t in usage.request_times if t > cutoff
                            ]
                
                logger.debug("quota_cleanup_completed")
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("quota_cleanup_error", error=str(e))
    
    def apply_quota_policy(
        self,
        policy_name: str,
        quotas: Dict[ResourceType, float],
        scope: QuotaScope = QuotaScope.AGENT,
        policy: QuotaPolicy = QuotaPolicy.HARD
    ):
        """Apply a quota policy to multiple scope IDs."""
        # This would be used to apply standard quota templates
        return {
            "policy_name": policy_name,
            "quotas": quotas,
            "scope": scope,
            "policy": policy
        }


class QuotaEnforcer:
    """Enforces quotas during resource allocation."""
    
    def __init__(self, quota_manager: QuotaManager):
        self.quota_manager = quota_manager
    
    async def check_all_quotas(
        self,
        requests: List[Tuple[QuotaScope, str, ResourceType, float]]
    ) -> Tuple[bool, List[str]]:
        """
        Check multiple quota requests.
        
        Returns:
            Tuple of (all_allowed, list_of_violations)
        """
        violations = []
        
        for scope, scope_id, resource_type, amount in requests:
            try:
                if not await self.quota_manager.check_quota(
                    scope, scope_id, resource_type, amount
                ):
                    violations.append(
                        f"{scope.value} '{scope_id}' exceeds "
                        f"{resource_type.name} quota"
                    )
            except QuotaExceededError as e:
                violations.append(str(e))
        
        return len(violations) == 0, violations
    
    async def allocate_with_quotas(
        self,
        allocations: List[Tuple[QuotaScope, str, ResourceType, float]]
    ) -> Tuple[bool, List[Tuple[QuotaScope, str, ResourceType, float]]]:
        """
        Allocate resources with quota enforcement.
        
        Returns:
            Tuple of (success, list_of_allocated_resources)
        """
        allocated = []
        
        try:
            # First check all quotas
            allowed, violations = await self.check_all_quotas(allocations)
            if not allowed:
                return False, []
            
            # Allocate all
            for scope, scope_id, resource_type, amount in allocations:
                await self.quota_manager.allocate(
                    scope, scope_id, resource_type, amount
                )
                allocated.append((scope, scope_id, resource_type, amount))
            
            return True, allocated
            
        except Exception as e:
            # Rollback allocations
            for scope, scope_id, resource_type, amount in allocated:
                await self.quota_manager.release(
                    scope, scope_id, resource_type, amount
                )
            
            logger.error("quota_allocation_failed", error=str(e))
            return False, []


# Predefined quota policies
class QuotaPolicies:
    """Common quota policies."""
    
    SMALL_AGENT = {
        ResourceType.CPU: 2.0,
        ResourceType.MEMORY: 512.0,
        ResourceType.IO: 10.0,
        ResourceType.NETWORK: 10.0
    }
    
    MEDIUM_AGENT = {
        ResourceType.CPU: 4.0,
        ResourceType.MEMORY: 2048.0,
        ResourceType.IO: 50.0,
        ResourceType.NETWORK: 50.0
    }
    
    LARGE_AGENT = {
        ResourceType.CPU: 8.0,
        ResourceType.MEMORY: 8192.0,
        ResourceType.IO: 100.0,
        ResourceType.NETWORK: 100.0
    }
    
    GPU_AGENT = {
        ResourceType.CPU: 4.0,
        ResourceType.MEMORY: 16384.0,
        ResourceType.GPU: 1.0,
        ResourceType.IO: 100.0,
        ResourceType.NETWORK: 100.0
    }