"""Event streaming and processing for workflow monitoring."""

import asyncio
from typing import Dict, List, Optional, Any, Callable, Set
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import json
import structlog
from collections import defaultdict
import weakref

from core.storage.events import WorkflowEvent, EventType


logger = structlog.get_logger(__name__)


@dataclass
class EventFilter:
    """Filter for selecting specific events."""
    workflow_ids: Optional[Set[str]] = None
    event_types: Optional[Set[EventType]] = None
    min_timestamp: Optional[datetime] = None
    max_timestamp: Optional[datetime] = None
    attributes: Optional[Dict[str, Any]] = None
    
    def matches(self, event: WorkflowEvent) -> bool:
        """Check if an event matches this filter."""
        if self.workflow_ids and event.workflow_id not in self.workflow_ids:
            return False
        
        if self.event_types and event.event_type not in self.event_types:
            return False
        
        if self.min_timestamp and event.timestamp < self.min_timestamp:
            return False
        
        if self.max_timestamp and event.timestamp > self.max_timestamp:
            return False
        
        if self.attributes:
            for key, value in self.attributes.items():
                if event.data.get(key) != value:
                    return False
        
        return True


@dataclass
class EventSubscription:
    """Subscription to event stream."""
    subscriber_id: str
    callback: Callable[[WorkflowEvent], None]
    filter: Optional[EventFilter] = None
    error_handler: Optional[Callable[[Exception], None]] = None
    is_async: bool = field(default=False, init=False)
    
    def __post_init__(self):
        self.is_async = asyncio.iscoroutinefunction(self.callback)


class EventProcessor:
    """Base class for event processors."""
    
    async def process(self, event: WorkflowEvent) -> Optional[WorkflowEvent]:
        """Process an event, optionally transforming it."""
        return event
    
    async def start(self) -> None:
        """Start the processor."""
        pass
    
    async def stop(self) -> None:
        """Stop the processor."""
        pass


class EventAggregator(EventProcessor):
    """Aggregates events over time windows."""
    
    def __init__(self, window_size: float = 60.0):
        self.window_size = window_size
        self.windows: Dict[str, List[WorkflowEvent]] = defaultdict(list)
        self._cleanup_task: Optional[asyncio.Task] = None
    
    async def start(self) -> None:
        """Start the aggregator."""
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
    
    async def stop(self) -> None:
        """Stop the aggregator."""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            await asyncio.gather(self._cleanup_task, return_exceptions=True)
    
    async def process(self, event: WorkflowEvent) -> Optional[WorkflowEvent]:
        """Add event to aggregation window."""
        window_key = self._get_window_key(event)
        self.windows[window_key].append(event)
        return event
    
    def _get_window_key(self, event: WorkflowEvent) -> str:
        """Get the window key for an event."""
        window_start = int(event.timestamp.timestamp() / self.window_size) * self.window_size
        return f"{event.workflow_id}:{window_start}"
    
    async def _cleanup_loop(self) -> None:
        """Clean up old windows."""
        while True:
            try:
                await asyncio.sleep(self.window_size)
                
                current_time = datetime.utcnow().timestamp()
                cutoff_time = current_time - (self.window_size * 2)
                
                # Remove old windows
                old_keys = [
                    key for key in self.windows
                    if float(key.split(':')[1]) < cutoff_time
                ]
                
                for key in old_keys:
                    del self.windows[key]
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("aggregator_cleanup_error", error=str(e))
    
    def get_aggregated_stats(self, workflow_id: str) -> Dict[str, Any]:
        """Get aggregated statistics for a workflow."""
        stats = {
            "total_events": 0,
            "events_by_type": defaultdict(int),
            "error_count": 0,
            "avg_state_duration": 0,
            "windows": []
        }
        
        # Aggregate across all windows for this workflow
        for key, events in self.windows.items():
            if key.startswith(f"{workflow_id}:"):
                stats["total_events"] += len(events)
                
                for event in events:
                    stats["events_by_type"][event.event_type.value] += 1
                    
                    if event.event_type == EventType.WORKFLOW_FAILED:
                        stats["error_count"] += 1
                
                stats["windows"].append({
                    "timestamp": float(key.split(':')[1]),
                    "event_count": len(events)
                })
        
        return dict(stats)


class EventStream:
    """Central event streaming service."""
    
    def __init__(self, buffer_size: int = 1000):
        self.buffer_size = buffer_size
        self._buffer: asyncio.Queue[WorkflowEvent] = asyncio.Queue(maxsize=buffer_size)
        self._subscriptions: Dict[str, EventSubscription] = {}
        self._processors: List[EventProcessor] = []
        self._running = False
        self._tasks: Set[asyncio.Task] = set()
        self._stats = {
            "events_received": 0,
            "events_delivered": 0,
            "delivery_errors": 0,
            "buffer_overflows": 0
        }
    
    async def start(self) -> None:
        """Start the event stream."""
        self._running = True
        
        # Start processors
        for processor in self._processors:
            await processor.start()
        
        # Start delivery task
        delivery_task = asyncio.create_task(self._delivery_loop())
        self._tasks.add(delivery_task)
        
        logger.info("event_stream_started", buffer_size=self.buffer_size)
    
    async def stop(self) -> None:
        """Stop the event stream."""
        self._running = False
        
        # Cancel all tasks
        for task in self._tasks:
            task.cancel()
        
        await asyncio.gather(*self._tasks, return_exceptions=True)
        
        # Stop processors
        for processor in self._processors:
            await processor.stop()
        
        logger.info("event_stream_stopped", stats=self._stats)
    
    async def publish(self, event: WorkflowEvent) -> None:
        """Publish an event to the stream."""
        if not self._running:
            return
        
        self._stats["events_received"] += 1
        
        try:
            await self._buffer.put_nowait(event)
        except asyncio.QueueFull:
            self._stats["buffer_overflows"] += 1
            # In production, could implement backpressure or spill to disk
            logger.warning(
                "event_buffer_overflow",
                workflow_id=event.workflow_id,
                event_type=event.event_type.value
            )
    
    def subscribe(
        self,
        callback: Callable[[WorkflowEvent], None],
        filter: Optional[EventFilter] = None,
        error_handler: Optional[Callable[[Exception], None]] = None
    ) -> str:
        """Subscribe to events."""
        subscriber_id = f"subscriber_{len(self._subscriptions)}"
        
        subscription = EventSubscription(
            subscriber_id=subscriber_id,
            callback=callback,
            filter=filter,
            error_handler=error_handler
        )
        
        self._subscriptions[subscriber_id] = subscription
        
        logger.debug(
            "event_subscription_added",
            subscriber_id=subscriber_id,
            has_filter=filter is not None
        )
        
        return subscriber_id
    
    def unsubscribe(self, subscriber_id: str) -> None:
        """Unsubscribe from events."""
        if subscriber_id in self._subscriptions:
            del self._subscriptions[subscriber_id]
            logger.debug("event_subscription_removed", subscriber_id=subscriber_id)
    
    def add_processor(self, processor: EventProcessor) -> None:
        """Add an event processor."""
        self._processors.append(processor)
    
    async def _delivery_loop(self) -> None:
        """Main event delivery loop."""
        while self._running:
            try:
                # Get event from buffer
                event = await asyncio.wait_for(
                    self._buffer.get(),
                    timeout=1.0
                )
                
                # Process event
                processed_event = event
                for processor in self._processors:
                    processed_event = await processor.process(processed_event)
                    if processed_event is None:
                        break
                
                if processed_event:
                    # Deliver to subscribers
                    await self._deliver_event(processed_event)
                
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("event_delivery_error", error=str(e))
    
    async def _deliver_event(self, event: WorkflowEvent) -> None:
        """Deliver event to all matching subscribers."""
        delivery_tasks = []
        
        for subscription in self._subscriptions.values():
            # Check filter
            if subscription.filter and not subscription.filter.matches(event):
                continue
            
            # Create delivery task
            task = asyncio.create_task(
                self._deliver_to_subscriber(event, subscription)
            )
            delivery_tasks.append(task)
        
        # Wait for all deliveries
        if delivery_tasks:
            await asyncio.gather(*delivery_tasks, return_exceptions=True)
    
    async def _deliver_to_subscriber(
        self,
        event: WorkflowEvent,
        subscription: EventSubscription
    ) -> None:
        """Deliver event to a single subscriber."""
        try:
            if subscription.is_async:
                await subscription.callback(event)
            else:
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    subscription.callback,
                    event
                )
            
            self._stats["events_delivered"] += 1
            
        except Exception as e:
            self._stats["delivery_errors"] += 1
            
            logger.error(
                "event_delivery_failed",
                subscriber_id=subscription.subscriber_id,
                error=str(e)
            )
            
            if subscription.error_handler:
                try:
                    subscription.error_handler(e)
                except Exception:
                    pass
    
    def get_stats(self) -> Dict[str, Any]:
        """Get event stream statistics."""
        return {
            **self._stats,
            "buffer_size": self._buffer.qsize(),
            "subscribers": len(self._subscriptions),
            "processors": len(self._processors)
        }


# Global event stream instance
_event_stream: Optional[EventStream] = None


def get_event_stream() -> EventStream:
    """Get the global event stream instance."""
    global _event_stream
    if not _event_stream:
        _event_stream = EventStream()
    return _event_stream


class EventSubscriber:
    """Context manager for event subscriptions."""
    
    def __init__(
        self,
        callback: Callable[[WorkflowEvent], None],
        filter: Optional[EventFilter] = None,
        stream: Optional[EventStream] = None
    ):
        self.callback = callback
        self.filter = filter
        self.stream = stream or get_event_stream()
        self.subscriber_id: Optional[str] = None
    
    def __enter__(self) -> str:
        self.subscriber_id = self.stream.subscribe(
            self.callback,
            self.filter
        )
        return self.subscriber_id
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.subscriber_id:
            self.stream.unsubscribe(self.subscriber_id)