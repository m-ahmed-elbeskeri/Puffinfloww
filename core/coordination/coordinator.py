"""Production-grade coordination system with comprehensive monitoring and control from paste-2.txt."""

from dataclasses import dataclass, field, asdict
from typing import Dict, Set, Optional, List, Any, Union, Callable
import asyncio
import time
import contextlib
from enum import Enum, auto
import threading
import weakref
import logging
from datetime import datetime, timedelta
import uuid

from core.coordination.primitives import CoordinationPrimitive, PrimitiveType, ResourceState
from core.coordination.rate_limiter import RateLimiter, RateLimitStrategy
from core.coordination.deadlock import DeadlockDetector


class AgentCoordinator:
    """Enhanced agent coordination system"""

    def __init__(
            self,
            agent: Any,
            detection_interval: float = 1.0,
            cleanup_interval: float = 60.0
    ):
        self.agent = weakref.proxy(agent)
        self.instance_id = str(uuid.uuid4())

        # Components
        self.rate_limiters: Dict[str, RateLimiter] = {}
        self.primitives: Dict[str, CoordinationPrimitive] = {}
        self.deadlock_detector = DeadlockDetector(
            agent,
            detection_interval=detection_interval
        )

        # State
        self._cleanup_task: Optional[asyncio.Task] = None
        self._cleanup_interval = cleanup_interval
        self._shutting_down = False

    async def start(self):
        """Start coordination system"""
        await self.deadlock_detector.start()
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def stop(self):
        """Stop coordination system"""
        self._shutting_down = True
        await self.deadlock_detector.stop()
        if self._cleanup_task:
            self._cleanup_task.cancel()
            await asyncio.shield(self._cleanup_task)

    def add_rate_limiter(
            self,
            name: str,
            max_rate: float,
            strategy: RateLimitStrategy = RateLimitStrategy.TOKEN_BUCKET,
            **kwargs
    ):
        """Add a rate limiter"""
        self.rate_limiters[name] = RateLimiter(
            max_rate=max_rate,
            strategy=strategy,
            **kwargs
        )

    def create_primitive(
            self,
            name: str,
            type: PrimitiveType,
            **kwargs
    ):
        """Create a coordination primitive"""
        self.primitives[name] = CoordinationPrimitive(
            name=name,
            type=type,
            **kwargs
        )

    async def coordinate_state_execution(
            self,
            state_name: str,
            timeout: Optional[float] = None
    ) -> bool:
        """Coordinate state execution"""
        try:
            # Check rate limits
            if state_name in self.rate_limiters:
                if not await self.rate_limiters[state_name].acquire():
                    if hasattr(self.agent, '_monitor'):
                        self.agent._monitor.logger.warning(
                            "rate_limit_exceeded",
                            state=state_name
                        )
                    return False

            # Check primitives
            for primitive in self.primitives.values():
                if not await primitive.acquire(
                        f"{self.instance_id}:{state_name}",
                        timeout=timeout
                ):
                    return False

            return True

        except Exception as e:
            if hasattr(self.agent, '_monitor'):
                self.agent._monitor.logger.error(
                    "coordination_error",
                    state=state_name,
                    error=str(e)
                )
            return False

    async def release_coordination(self, state_name: str):
        """Release coordination resources"""
        for primitive in self.primitives.values():
            await primitive.release(f"{self.instance_id}:{state_name}")

    def get_status(self) -> Dict[str, Any]:
        """Get coordinator status"""
        return {
            "instance_id": self.instance_id,
            "primitives": {
                name: prim.get_state()
                for name, prim in self.primitives.items()
            },
            "deadlock_detector": self.deadlock_detector.get_status(),
            "shutting_down": self._shutting_down
        }

    async def _cleanup_loop(self):
        """Cleanup loop for maintenance"""
        while not self._shutting_down:
            try:
                for primitive in self.primitives.values():
                    async with primitive._lock:
                        primitive._cleanup_expired()

                await asyncio.sleep(self._cleanup_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                if hasattr(self.agent, '_monitor'):
                    self.agent._monitor.logger.error(
                        "cleanup_error",
                        error=str(e)
                    )


def enhance_agent(agent: Any) -> Any:
    """Add production coordination to an agent"""

    # Add coordinator
    coordinator = AgentCoordinator(agent)
    agent._coordinator = coordinator

    # Start coordinator
    async def start_coordinator():
        await coordinator.start()

    if hasattr(agent, '_startup_tasks'):
        agent._startup_tasks.append(start_coordinator())
    else:
        asyncio.create_task(start_coordinator())

    # Enhance run_state
    original_run_state = agent.run_state

    async def enhanced_run_state(self, state_name: str) -> None:
        """Enhanced state execution with coordination"""
        # Track attempt
        attempt_id = str(uuid.uuid4())
        start_time = time.time()

        try:
            # Check coordination and rate limits
            if not await self._coordinator.coordinate_state_execution(state_name):
                if hasattr(self, '_monitor'):
                    self._monitor.logger.warning(
                        "coordination_failed",
                        state=state_name,
                        attempt=attempt_id
                    )
                # Requeue with backoff
                self._add_to_queue(
                    state_name,
                    self.state_metadata[state_name],
                    priority_boost=-1
                )
                return

            # Execute original state with monitoring
            if hasattr(self, '_monitor'):
                self._monitor.logger.info(
                    "state_execution_started",
                    state=state_name,
                    attempt=attempt_id,
                    metadata={
                        "resources": asdict(self.state_metadata[state_name].resources),
                        "dependencies": len(self.state_metadata[state_name].dependencies),
                        "attempts": self.state_metadata[state_name].attempts
                    }
                )

            async with self._execution_span(state_name, attempt_id):
                await original_run_state(state_name)

            # Record success metrics
            if hasattr(self, '_monitor'):
                duration = time.time() - start_time
                await self._monitor.record_metric(
                    'state_duration',
                    duration,
                    {'state': state_name, 'status': 'success'}
                )
                await self._monitor.record_metric(
                    'state_success',
                    1,
                    {'state': state_name}
                )

        except Exception as e:
            # Handle failure with monitoring
            if hasattr(self, '_monitor'):
                duration = time.time() - start_time
                self._monitor.logger.error(
                    "state_execution_failed",
                    state=state_name,
                    attempt=attempt_id,
                    error=str(e),
                    duration=duration
                )
                await self._monitor.record_metric(
                    'state_duration',
                    duration,
                    {'state': state_name, 'status': 'error'}
                )
                await self._monitor.record_metric(
                    'state_error',
                    1,
                    {'state': state_name, 'error_type': type(e).__name__}
                )
            raise

        finally:
            # Always release coordination
            await self._coordinator.release_coordination(state_name)

    agent.run_state = enhanced_run_state

    @contextlib.asynccontextmanager
    async def _execution_span(self, state_name: str, attempt_id: str):
        """Create execution span for monitoring"""
        if hasattr(self, '_monitor'):
            async with self._monitor.monitor_operation(
                    "state_execution",
                    {
                        "state": state_name,
                        "attempt": attempt_id,
                        "agent": self.name
                    }
            ) as span:
                yield span
        else:
            yield None

    agent._execution_span = _execution_span

    # Enhanced cleanup
    original_cleanup = getattr(agent, '_cleanup', None)

    async def enhanced_cleanup():
        """Enhanced cleanup with monitoring"""
        try:
            # Stop coordinator
            await agent._coordinator.stop()

            # Run original cleanup
            if original_cleanup:
                await original_cleanup()

        except Exception as e:
            if hasattr(agent, '_monitor'):
                agent._monitor.logger.error(
                    "cleanup_error",
                    error=str(e)
                )
            raise

    agent._cleanup = enhanced_cleanup

    # Add utility methods
    async def get_coordination_status(self) -> Dict[str, Any]:
        """Get coordination system status"""
        return agent._coordinator.get_status()

    agent.get_coordination_status = get_coordination_status

    async def reset_coordination(self):
        """Reset coordination system"""
        await agent._coordinator.stop()
        agent._coordinator = AgentCoordinator(agent)
        await agent._coordinator.start()

    agent.reset_coordination = reset_coordination

    # Add rate limiting utility
    def add_state_rate_limit(
            self,
            state_name: str,
            max_rate: float,
            strategy: RateLimitStrategy = RateLimitStrategy.TOKEN_BUCKET,
            **kwargs
    ):
        """Add rate limit for specific state"""
        self._coordinator.add_rate_limiter(
            state_name,
            max_rate,
            strategy,
            **kwargs
        )

    agent.add_state_rate_limit = add_state_rate_limit

    # Add coordination primitive utility
    def add_state_coordination(
            self,
            state_name: str,
            primitive_type: PrimitiveType,
            **kwargs
    ):
        """Add coordination primitive for specific state"""
        self._coordinator.create_primitive(
            f"state_{state_name}",
            primitive_type,
            **kwargs
        )

    agent.add_state_coordination = add_state_coordination

    return agent


# Helper function for easy agent enhancement
def create_coordinated_agent(
        name: str,
        **agent_kwargs
) -> Any:
    """Create an agent with coordination enabled"""
    from core.agent import Agent

    agent = Agent(name, **agent_kwargs)
    return enhance_agent(agent)