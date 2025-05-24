"""State lifecycle hooks for workflow execution."""

import asyncio
from typing import Dict, Any, Optional, Callable, List, Union
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from functools import wraps
import structlog
import contextvars
import logging


from core.agent.base import Agent, StateFunction
from core.agent.context import Context
from core.agent.state import StateStatus, StateMetadata


logger = structlog.get_logger(__name__)


class HookType(Enum):
    """Types of lifecycle hooks."""
    # Pre-execution hooks
    BEFORE_STATE = "before_state"
    BEFORE_RETRY = "before_retry"
    BEFORE_TRANSITION = "before_transition"
    
    # Post-execution hooks
    AFTER_STATE = "after_state"
    AFTER_SUCCESS = "after_success"
    AFTER_FAILURE = "after_failure"
    AFTER_RETRY = "after_retry"
    AFTER_TIMEOUT = "after_timeout"
    
    # Resource hooks
    BEFORE_RESOURCE_ACQUIRE = "before_resource_acquire"
    AFTER_RESOURCE_ACQUIRE = "after_resource_acquire"
    BEFORE_RESOURCE_RELEASE = "before_resource_release"
    AFTER_RESOURCE_RELEASE = "after_resource_release"
    
    # Checkpoint hooks
    BEFORE_CHECKPOINT = "before_checkpoint"
    AFTER_CHECKPOINT = "after_checkpoint"
    BEFORE_RESTORE = "before_restore"
    AFTER_RESTORE = "after_restore"


@dataclass
class HookContext:
    """Context passed to lifecycle hooks."""
    hook_type: HookType
    state_name: str
    agent_name: str
    timestamp: datetime = field(default_factory=datetime.utcnow)
    metadata: StateMetadata = None
    context: Optional[Context] = None
    error: Optional[Exception] = None
    result: Any = None
    retry_count: int = 0
    duration: Optional[float] = None
    extra: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "hook_type": self.hook_type.value,
            "state_name": self.state_name,
            "agent_name": self.agent_name,
            "timestamp": self.timestamp.isoformat(),
            "retry_count": self.retry_count,
            "duration": self.duration,
            "has_error": self.error is not None,
            "error_type": type(self.error).__name__ if self.error else None,
            "extra": self.extra
        }


# Context variable for current hook context
_current_hook_context: contextvars.ContextVar[Optional[HookContext]] = \
    contextvars.ContextVar('current_hook_context', default=None)


class LifecycleHook:
    """Base class for lifecycle hooks."""
    
    def __init__(self, name: str):
        self.name = name
        self.enabled = True
    
    async def execute(self, context: HookContext) -> None:
        """Execute the hook."""
        if not self.enabled:
            return
        
        try:
            await self._execute(context)
        except Exception as e:
            logger.error(
                "lifecycle_hook_error",
                hook_name=self.name,
                hook_type=context.hook_type.value,
                state_name=context.state_name,
                error=str(e)
            )
    
    async def _execute(self, context: HookContext) -> None:
        """Override in subclasses."""
        pass


class LoggingHook(LifecycleHook):
    """Hook that logs lifecycle events."""
    
    def __init__(self, name: str = "logging_hook", level: int = logging.INFO):
        super().__init__(name)
        self.level = level
        self.logger = structlog.get_logger(name)
    
    async def _execute(self, context: HookContext) -> None:
        """Log the lifecycle event."""
        self.logger.log(
            self.level,
            f"lifecycle_{context.hook_type.value}",
            **context.to_dict()
        )


class MetricsHook(LifecycleHook):
    """Hook that records metrics."""
    
    def __init__(self, name: str = "metrics_hook", metrics_collector=None):
        super().__init__(name)
        self.metrics_collector = metrics_collector
    
    async def _execute(self, context: HookContext) -> None:
        """Record metrics for the event."""
        if not self.metrics_collector:
            return
        
        # Record different metrics based on hook type
        if context.hook_type == HookType.AFTER_STATE:
            if context.duration:
                self.metrics_collector.record_state_execution(
                    agent=context.agent_name,
                    state=context.state_name,
                    duration=context.duration,
                    status="success" if not context.error else "error"
                )
        
        elif context.hook_type == HookType.AFTER_FAILURE:
            self.metrics_collector.record_error(
                agent=context.agent_name,
                state=context.state_name,
                error_type=type(context.error).__name__ if context.error else "unknown"
            )
        
        elif context.hook_type == HookType.AFTER_RETRY:
            # Could record retry metrics
            pass


class ValidationHook(LifecycleHook):
    """Hook that validates state inputs/outputs."""
    
    def __init__(
        self,
        name: str = "validation_hook",
        validators: Optional[Dict[str, Callable]] = None
    ):
        super().__init__(name)
        self.validators = validators or {}
    
    async def _execute(self, context: HookContext) -> None:
        """Validate based on hook type."""
        if context.hook_type == HookType.BEFORE_STATE:
            await self._validate_inputs(context)
        elif context.hook_type == HookType.AFTER_SUCCESS:
            await self._validate_outputs(context)
    
    async def _validate_inputs(self, context: HookContext) -> None:
        """Validate state inputs."""
        validator = self.validators.get(f"{context.state_name}_input")
        if validator and context.context:
            try:
                if asyncio.iscoroutinefunction(validator):
                    await validator(context.context)
                else:
                    validator(context.context)
            except Exception as e:
                raise ValueError(f"Input validation failed: {str(e)}")
    
    async def _validate_outputs(self, context: HookContext) -> None:
        """Validate state outputs."""
        validator = self.validators.get(f"{context.state_name}_output")
        if validator and context.context:
            try:
                if asyncio.iscoroutinefunction(validator):
                    await validator(context.context)
                else:
                    validator(context.context)
            except Exception as e:
                raise ValueError(f"Output validation failed: {str(e)}")


class CallbackHook(LifecycleHook):
    """Hook that calls user-defined callbacks."""
    
    def __init__(
        self,
        name: str,
        callback: Callable[[HookContext], None]
    ):
        super().__init__(name)
        self.callback = callback
    
    async def _execute(self, context: HookContext) -> None:
        """Execute the callback."""
        if asyncio.iscoroutinefunction(self.callback):
            await self.callback(context)
        else:
            await asyncio.get_event_loop().run_in_executor(
                None,
                self.callback,
                context
            )


class StateLifecycle:
    """Manages lifecycle hooks for states."""
    
    def __init__(self):
        self._hooks: Dict[HookType, List[LifecycleHook]] = {
            hook_type: [] for hook_type in HookType
        }
        self._state_hooks: Dict[str, Dict[HookType, List[LifecycleHook]]] = {}
    
    def add_hook(
        self,
        hook: LifecycleHook,
        hook_types: Union[HookType, List[HookType]],
        state_names: Optional[Union[str, List[str]]] = None
    ) -> None:
        """Add a lifecycle hook."""
        if isinstance(hook_types, HookType):
            hook_types = [hook_types]
        
        if state_names is None:
            # Global hook
            for hook_type in hook_types:
                self._hooks[hook_type].append(hook)
        else:
            # State-specific hook
            if isinstance(state_names, str):
                state_names = [state_names]
            
            for state_name in state_names:
                if state_name not in self._state_hooks:
                    self._state_hooks[state_name] = {
                        ht: [] for ht in HookType
                    }
                
                for hook_type in hook_types:
                    self._state_hooks[state_name][hook_type].append(hook)
    
    def remove_hook(self, hook: LifecycleHook) -> None:
        """Remove a lifecycle hook."""
        # Remove from global hooks
        for hooks in self._hooks.values():
            if hook in hooks:
                hooks.remove(hook)
        
        # Remove from state-specific hooks
        for state_hooks in self._state_hooks.values():
            for hooks in state_hooks.values():
                if hook in hooks:
                    hooks.remove(hook)
    
    async def execute_hooks(
        self,
        hook_type: HookType,
        context: HookContext
    ) -> None:
        """Execute all relevant hooks."""
        # Set context variable
        token = _current_hook_context.set(context)
        
        try:
            # Execute global hooks
            for hook in self._hooks[hook_type]:
                await hook.execute(context)
            
            # Execute state-specific hooks
            if context.state_name in self._state_hooks:
                for hook in self._state_hooks[context.state_name][hook_type]:
                    await hook.execute(context)
        finally:
            _current_hook_context.reset(token)
    
    def create_hook_context(
        self,
        hook_type: HookType,
        state_name: str,
        agent_name: str,
        **kwargs
    ) -> HookContext:
        """Create a hook context."""
        return HookContext(
            hook_type=hook_type,
            state_name=state_name,
            agent_name=agent_name,
            **kwargs
        )


def get_current_hook_context() -> Optional[HookContext]:
    """Get the current hook context."""
    return _current_hook_context.get()


def lifecycle_hook(
    hook_type: HookType,
    state_names: Optional[Union[str, List[str]]] = None
) -> Callable:
    """
    Decorator to add lifecycle hooks to functions.
    
    Example:
        @lifecycle_hook(HookType.BEFORE_STATE)
        async def validate_inputs(context: HookContext):
            # Validation logic
            pass
    """
    def decorator(func: Callable) -> Callable:
        # Create callback hook
        hook = CallbackHook(
            name=f"{func.__name__}_hook",
            callback=func
        )
        
        # Register with global lifecycle (would need access to it)
        # This is a simplified version
        return func
    return decorator


class StateLifecycleManager:
    """Manages lifecycle for all states in an agent."""
    
    def __init__(self, agent: Agent):
        self.agent = agent
        self.lifecycle = StateLifecycle()
        self._original_methods = {}
        self._setup_lifecycle()
    
    def _setup_lifecycle(self):
        """Setup lifecycle hooks for agent."""
        # Add default hooks
        self.lifecycle.add_hook(
            LoggingHook(),
            list(HookType)  # All hook types
        )
        
        # Wrap agent methods
        self._wrap_run_state()
    
    def _wrap_run_state(self):
        """Wrap agent's run_state method with lifecycle hooks."""
        original_run_state = self.agent.run_state
        lifecycle = self.lifecycle
        
        async def wrapped_run_state(state_name: str) -> None:
            """Enhanced run_state with lifecycle hooks."""
            start_time = time.time()
            context = None
            error = None
            
            try:
                # Before state hook
                hook_ctx = lifecycle.create_hook_context(
                    HookType.BEFORE_STATE,
                    state_name,
                    self.agent.name,
                    metadata=self.agent.state_metadata.get(state_name)
                )
                await lifecycle.execute_hooks(HookType.BEFORE_STATE, hook_ctx)
                
                # Run original method
                result = await original_run_state(state_name)
                
                # After success hook
                duration = time.time() - start_time
                hook_ctx = lifecycle.create_hook_context(
                    HookType.AFTER_SUCCESS,
                    state_name,
                    self.agent.name,
                    result=result,
                    duration=duration
                )
                await lifecycle.execute_hooks(HookType.AFTER_SUCCESS, hook_ctx)
                
                # After state hook
                hook_ctx = lifecycle.create_hook_context(
                    HookType.AFTER_STATE,
                    state_name,
                    self.agent.name,
                    result=result,
                    duration=duration
                )
                await lifecycle.execute_hooks(HookType.AFTER_STATE, hook_ctx)
                
                return result
                
            except Exception as e:
                error = e
                duration = time.time() - start_time
                
                # After failure hook
                hook_ctx = lifecycle.create_hook_context(
                    HookType.AFTER_FAILURE,
                    state_name,
                    self.agent.name,
                    error=e,
                    duration=duration
                )
                await lifecycle.execute_hooks(HookType.AFTER_FAILURE, hook_ctx)
                
                # After state hook (even on failure)
                hook_ctx = lifecycle.create_hook_context(
                    HookType.AFTER_STATE,
                    state_name,
                    self.agent.name,
                    error=e,
                    duration=duration
                )
                await lifecycle.execute_hooks(HookType.AFTER_STATE, hook_ctx)
                
                raise
        
        self.agent.run_state = wrapped_run_state
        self._original_methods['run_state'] = original_run_state
    
    def add_hook(
        self,
        hook: LifecycleHook,
        hook_types: Union[HookType, List[HookType]],
        state_names: Optional[Union[str, List[str]]] = None
    ) -> None:
        """Add a lifecycle hook."""
        self.lifecycle.add_hook(hook, hook_types, state_names)
    
    def remove_hook(self, hook: LifecycleHook) -> None:
        """Remove a lifecycle hook."""
        self.lifecycle.remove_hook(hook)
    
    def restore_original_methods(self):
        """Restore original agent methods."""
        for method_name, original_method in self._original_methods.items():
            setattr(self.agent, method_name, original_method)


# Example hooks for common scenarios
class TimingHook(LifecycleHook):
    """Records timing information for states."""
    
    def __init__(self):
        super().__init__("timing_hook")
        self._start_times: Dict[str, float] = {}
    
    async def _execute(self, context: HookContext) -> None:
        """Record timing information."""
        if context.hook_type == HookType.BEFORE_STATE:
            self._start_times[context.state_name] = time.time()
        
        elif context.hook_type == HookType.AFTER_STATE:
            if context.state_name in self._start_times:
                duration = time.time() - self._start_times[context.state_name]
                logger.info(
                    "state_timing",
                    state_name=context.state_name,
                    duration=duration,
                    status="success" if not context.error else "error"
                )
                del self._start_times[context.state_name]


class RetryHook(LifecycleHook):
    """Handles retry logic with exponential backoff."""
    
    def __init__(self, max_retries: int = 3, base_delay: float = 1.0):
        super().__init__("retry_hook")
        self.max_retries = max_retries
        self.base_delay = base_delay
        self._retry_counts: Dict[str, int] = {}
    
    async def _execute(self, context: HookContext) -> None:
        """Handle retry logic."""
        if context.hook_type == HookType.BEFORE_RETRY:
            retry_count = self._retry_counts.get(context.state_name, 0)
            
            if retry_count >= self.max_retries:
                raise Exception(f"Max retries ({self.max_retries}) exceeded")
            
            # Calculate delay with exponential backoff
            delay = self.base_delay * (2 ** retry_count)
            
            logger.info(
                "retry_delay",
                state_name=context.state_name,
                retry_count=retry_count,
                delay=delay
            )
            
            await asyncio.sleep(delay)
            self._retry_counts[context.state_name] = retry_count + 1
        
        elif context.hook_type == HookType.AFTER_SUCCESS:
            # Reset retry count on success
            self._retry_counts.pop(context.state_name, None)


import time