"""Queue management for scheduled tasks."""

import asyncio
from typing import Dict, List, Optional, Any, Generic, TypeVar, Protocol
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
import heapq
import time
import structlog
from collections import defaultdict, deque
import json

from core.scheduler.base import ScheduledTask


logger = structlog.get_logger(__name__)


T = TypeVar('T')


class QueueStatus(Enum):
    """Queue status."""
    ACTIVE = "active"
    PAUSED = "paused"
    DRAINING = "draining"  # Not accepting new items
    STOPPED = "stopped"


@dataclass
class QueueStats:
    """Queue statistics."""
    name: str
    size: int
    capacity: Optional[int]
    enqueued_total: int
    dequeued_total: int
    dropped_total: int
    avg_wait_time: float
    max_wait_time: float
    oldest_item_age: Optional[float]
    status: QueueStatus
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "name": self.name,
            "size": self.size,
            "capacity": self.capacity,
            "enqueued_total": self.enqueued_total,
            "dequeued_total": self.dequeued_total,
            "dropped_total": self.dropped_total,
            "avg_wait_time": self.avg_wait_time,
            "max_wait_time": self.max_wait_time,
            "oldest_item_age": self.oldest_item_age,
            "status": self.status.value
        }


class TaskQueue(Protocol[T]):
    """Protocol for task queues."""
    
    async def enqueue(self, item: T) -> bool:
        """Add item to queue."""
        ...
    
    async def dequeue(self) -> Optional[T]:
        """Remove and return item from queue."""
        ...
    
    async def peek(self) -> Optional[T]:
        """View next item without removing."""
        ...
    
    def size(self) -> int:
        """Get current queue size."""
        ...
    
    def is_empty(self) -> bool:
        """Check if queue is empty."""
        ...
    
    def get_stats(self) -> QueueStats:
        """Get queue statistics."""
        ...


class PriorityQueue(Generic[T]):
    """Priority queue implementation."""
    
    def __init__(
        self,
        name: str,
        capacity: Optional[int] = None,
        drop_oldest: bool = False
    ):
        self.name = name
        self.capacity = capacity
        self.drop_oldest = drop_oldest
        
        self._queue: List[Tuple[float, int, T]] = []  # (priority, counter, item)
        self._counter = 0  # For stable sorting
        self._lock = asyncio.Lock()
        self._not_empty = asyncio.Condition()
        
        # Statistics
        self._enqueued_total = 0
        self._dequeued_total = 0
        self._dropped_total = 0
        self._wait_times: deque = deque(maxlen=1000)
        self._enqueue_times: Dict[int, float] = {}
        
        self.status = QueueStatus.ACTIVE
    
    async def enqueue(self, item: T, priority: float = 0.0) -> bool:
        """Add item to queue with priority (higher = higher priority)."""
        if self.status in (QueueStatus.DRAINING, QueueStatus.STOPPED):
            return False
        
        async with self._lock:
            # Check capacity
            if self.capacity and len(self._queue) >= self.capacity:
                if self.drop_oldest:
                    # Drop lowest priority item
                    if self._queue:
                        _, counter, _ = heapq.heappop(self._queue)
                        self._enqueue_times.pop(counter, None)
                        self._dropped_total += 1
                else:
                    return False
            
            # Add item (negate priority for min heap)
            self._counter += 1
            heapq.heappush(self._queue, (-priority, self._counter, item))
            self._enqueue_times[self._counter] = time.time()
            self._enqueued_total += 1
            
            # Notify waiters
            async with self._not_empty:
                self._not_empty.notify()
            
            return True
    
    async def dequeue(self, timeout: Optional[float] = None) -> Optional[T]:
        """Remove and return highest priority item."""
        if self.status == QueueStatus.STOPPED:
            return None
        
        start_time = time.time()
        
        async with self._not_empty:
            while not self._queue and self.status == QueueStatus.ACTIVE:
                try:
                    await asyncio.wait_for(self._not_empty.wait(), timeout)
                except asyncio.TimeoutError:
                    return None
            
            if not self._queue:
                return None
            
            async with self._lock:
                _, counter, item = heapq.heappop(self._queue)
                
                # Calculate wait time
                enqueue_time = self._enqueue_times.pop(counter, start_time)
                wait_time = time.time() - enqueue_time
                self._wait_times.append(wait_time)
                
                self._dequeued_total += 1
                
                return item
    
    async def peek(self) -> Optional[T]:
        """View highest priority item without removing."""
        async with self._lock:
            if self._queue:
                _, _, item = self._queue[0]
                return item
            return None
    
    def size(self) -> int:
        """Get current queue size."""
        return len(self._queue)
    
    def is_empty(self) -> bool:
        """Check if queue is empty."""
        return len(self._queue) == 0
    
    async def clear(self) -> int:
        """Clear all items from queue."""
        async with self._lock:
            count = len(self._queue)
            self._queue.clear()
            self._enqueue_times.clear()
            return count
    
    def get_stats(self) -> QueueStats:
        """Get queue statistics."""
        avg_wait = sum(self._wait_times) / len(self._wait_times) if self._wait_times else 0
        max_wait = max(self._wait_times) if self._wait_times else 0
        
        oldest_age = None
        if self._enqueue_times:
            oldest_time = min(self._enqueue_times.values())
            oldest_age = time.time() - oldest_time
        
        return QueueStats(
            name=self.name,
            size=self.size(),
            capacity=self.capacity,
            enqueued_total=self._enqueued_total,
            dequeued_total=self._dequeued_total,
            dropped_total=self._dropped_total,
            avg_wait_time=avg_wait,
            max_wait_time=max_wait,
            oldest_item_age=oldest_age,
            status=self.status
        )
    
    def pause(self) -> None:
        """Pause queue (stop dequeuing)."""
        self.status = QueueStatus.PAUSED
    
    def resume(self) -> None:
        """Resume queue."""
        self.status = QueueStatus.ACTIVE
        # Notify waiters
        asyncio.create_task(self._notify_all())
    
    async def _notify_all(self) -> None:
        """Notify all waiters."""
        async with self._not_empty:
            self._not_empty.notify_all()
    
    def start_draining(self) -> None:
        """Start draining (no new items)."""
        self.status = QueueStatus.DRAINING
    
    def stop(self) -> None:
        """Stop queue completely."""
        self.status = QueueStatus.STOPPED
        asyncio.create_task(self._notify_all())


class DelayedQueue(PriorityQueue[T]):
    """Queue that delays items until a specific time."""
    
    def __init__(self, name: str, capacity: Optional[int] = None):
        super().__init__(name, capacity)
        self._processor_task: Optional[asyncio.Task] = None
        self._ready_queue: asyncio.Queue = asyncio.Queue()
    
    async def start(self) -> None:
        """Start the delayed queue processor."""
        if not self._processor_task:
            self._processor_task = asyncio.create_task(self._process_delayed())
    
    async def stop(self) -> None:
        """Stop the delayed queue processor."""
        super().stop()
        if self._processor_task:
            self._processor_task.cancel()
            await asyncio.gather(self._processor_task, return_exceptions=True)
    
    async def enqueue_delayed(
        self,
        item: T,
        delay: Union[float, timedelta]
    ) -> bool:
        """Add item to queue with delay."""
        if isinstance(delay, timedelta):
            delay = delay.total_seconds()
        
        # Use timestamp as priority (earlier = higher priority)
        run_at = time.time() + delay
        return await self.enqueue(item, priority=-run_at)
    
    async def enqueue_at(self, item: T, when: datetime) -> bool:
        """Add item to queue to run at specific time."""
        run_at = when.timestamp()
        return await self.enqueue(item, priority=-run_at)
    
    async def dequeue(self, timeout: Optional[float] = None) -> Optional[T]:
        """Get next ready item."""
        try:
            return await asyncio.wait_for(self._ready_queue.get(), timeout)
        except asyncio.TimeoutError:
            return None
    
    async def _process_delayed(self) -> None:
        """Process delayed items."""
        while self.status != QueueStatus.STOPPED:
            try:
                # Check for ready items
                now = time.time()
                
                while True:
                    async with self._lock:
                        if not self._queue:
                            break
                        
                        # Peek at next item
                        neg_priority, counter, item = self._queue[0]
                        run_at = -neg_priority
                        
                        if run_at <= now:
                            # Item is ready
                            heapq.heappop(self._queue)
                            self._enqueue_times.pop(counter, None)
                            await self._ready_queue.put(item)
                        else:
                            # Wait until next item is ready
                            wait_time = run_at - now
                            break
                
                # Sleep until next item or 1 second
                await asyncio.sleep(min(wait_time if 'wait_time' in locals() else 1, 1))
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("delayed_queue_error", error=str(e))
                await asyncio.sleep(1)


class DeadLetterQueue(PriorityQueue[T]):
    """Queue for failed items."""
    
    def __init__(
        self,
        name: str,
        capacity: Optional[int] = None,
        max_retries: int = 3,
        retry_delay: float = 60.0
    ):
        super().__init__(name, capacity, drop_oldest=True)
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self._retry_counts: Dict[str, int] = defaultdict(int)
        self._original_queue: Optional[TaskQueue] = None
    
    def set_original_queue(self, queue: TaskQueue) -> None:
        """Set the original queue for retry."""
        self._original_queue = queue
    
    async def enqueue_failed(
        self,
        item: T,
        error: str,
        retry: bool = True
    ) -> bool:
        """Add failed item to DLQ."""
        item_id = str(id(item))  # Simple ID, could be enhanced
        
        # Track retry count
        self._retry_counts[item_id] += 1
        
        # Add error info
        if hasattr(item, 'metadata'):
            if not item.metadata:
                item.metadata = {}
            item.metadata['dlq_error'] = error
            item.metadata['dlq_retry_count'] = self._retry_counts[item_id]
            item.metadata['dlq_timestamp'] = datetime.utcnow().isoformat()
        
        # Check if should retry
        if retry and self._retry_counts[item_id] <= self.max_retries and self._original_queue:
            # Schedule retry
            asyncio.create_task(self._schedule_retry(item, item_id))
            return True
        else:
            # Add to DLQ
            return await self.enqueue(item)
    
    async def _schedule_retry(self, item: T, item_id: str) -> None:
        """Schedule item retry."""
        retry_count = self._retry_counts[item_id]
        
        # Exponential backoff
        delay = self.retry_delay * (2 ** (retry_count - 1))
        
        logger.info(
            "scheduling_retry",
            item_id=item_id,
            retry_count=retry_count,
            delay=delay
        )
        
        await asyncio.sleep(delay)
        
        # Re-enqueue to original queue
        if self._original_queue:
            success = await self._original_queue.enqueue(item)
            if not success:
                # If can't enqueue, add to DLQ
                await self.enqueue(item)
    
    async def reprocess_all(self) -> int:
        """Reprocess all items in DLQ."""
        if not self._original_queue:
            return 0
        
        count = 0
        items = []
        
        # Drain DLQ
        while not self.is_empty():
            item = await self.dequeue()
            if item:
                items.append(item)
        
        # Re-enqueue all
        for item in items:
            # Reset retry count
            item_id = str(id(item))
            self._retry_counts[item_id] = 0
            
            if await self._original_queue.enqueue(item):
                count += 1
            else:
                # Put back in DLQ
                await self.enqueue(item)
        
        return count


class QueueManager:
    """Manages multiple queues."""
    
    def __init__(self):
        self._queues: Dict[str, TaskQueue] = {}
        self._dlqs: Dict[str, DeadLetterQueue] = {}
        self._stats_interval = 60.0  # seconds
        self._stats_task: Optional[asyncio.Task] = None
    
    async def start(self) -> None:
        """Start queue manager."""
        self._stats_task = asyncio.create_task(self._collect_stats())
        
        # Start any delayed queues
        for queue in self._queues.values():
            if isinstance(queue, DelayedQueue):
                await queue.start()
    
    async def stop(self) -> None:
        """Stop queue manager."""
        if self._stats_task:
            self._stats_task.cancel()
            await asyncio.gather(self._stats_task, return_exceptions=True)
        
        # Stop all queues
        for queue in self._queues.values():
            if hasattr(queue, 'stop'):
                await queue.stop()
    
    def create_queue(
        self,
        name: str,
        queue_type: str = "priority",
        **kwargs
    ) -> TaskQueue:
        """Create a new queue."""
        if name in self._queues:
            raise ValueError(f"Queue {name} already exists")
        
        queue_types = {
            "priority": PriorityQueue,
            "delayed": DelayedQueue,
            "dlq": DeadLetterQueue
        }
        
        queue_class = queue_types.get(queue_type, PriorityQueue)
        queue = queue_class(name, **kwargs)
        
        self._queues[name] = queue
        
        # Create associated DLQ if not a DLQ itself
        if queue_type != "dlq":
            dlq = DeadLetterQueue(f"{name}_dlq", capacity=10000)
            dlq.set_original_queue(queue)
            self._dlqs[name] = dlq
            self._queues[f"{name}_dlq"] = dlq
        
        logger.info(
            "queue_created",
            name=name,
            type=queue_type
        )
        
        return queue
    
    def get_queue(self, name: str) -> Optional[TaskQueue]:
        """Get queue by name."""
        return self._queues.get(name)
    
    def get_dlq(self, queue_name: str) -> Optional[DeadLetterQueue]:
        """Get DLQ for a queue."""
        return self._dlqs.get(queue_name)
    
    def list_queues(self) -> List[str]:
        """List all queue names."""
        return list(self._queues.keys())
    
    def get_all_stats(self) -> Dict[str, QueueStats]:
        """Get stats for all queues."""
        return {
            name: queue.get_stats()
            for name, queue in self._queues.items()
        }
    
    async def _collect_stats(self) -> None:
        """Periodically collect queue statistics."""
        while True:
            try:
                await asyncio.sleep(self._stats_interval)
                
                stats = self.get_all_stats()
                
                # Log stats
                for name, stat in stats.items():
                    if stat.size > 0:
                        logger.info(
                            "queue_stats",
                            queue=name,
                            size=stat.size,
                            enqueued=stat.enqueued_total,
                            dequeued=stat.dequeued_total,
                            avg_wait=stat.avg_wait_time
                        )
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("stats_collection_error", error=str(e))
    
    async def move_items(
        self,
        from_queue: str,
        to_queue: str,
        count: Optional[int] = None,
        filter_func: Optional[Callable[[Any], bool]] = None
    ) -> int:
        """Move items between queues."""
        source = self.get_queue(from_queue)
        dest = self.get_queue(to_queue)
        
        if not source or not dest:
            raise ValueError("Invalid queue names")
        
        moved = 0
        items_to_move = []
        
        # Collect items
        while (count is None or moved < count) and not source.is_empty():
            item = await source.dequeue()
            if item is None:
                break
            
            if filter_func is None or filter_func(item):
                items_to_move.append(item)
                moved += 1
            else:
                # Put back items that don't match filter
                await source.enqueue(item)
        
        # Move to destination
        for item in items_to_move:
            if not await dest.enqueue(item):
                # If can't enqueue, put back in source
                await source.enqueue(item)
                moved -= 1
        
        logger.info(
            "items_moved",
            from_queue=from_queue,
            to_queue=to_queue,
            count=moved
        )
        
        return moved


# Queue routing
class QueueRouter:
    """Routes items to appropriate queues."""
    
    def __init__(self, queue_manager: QueueManager):
        self.queue_manager = queue_manager
        self._routes: List[Tuple[Callable[[Any], bool], str]] = []
        self._default_queue: Optional[str] = None
    
    def add_route(
        self,
        predicate: Callable[[Any], bool],
        queue_name: str
    ) -> None:
        """Add routing rule."""
        self._routes.append((predicate, queue_name))
    
    def set_default_queue(self, queue_name: str) -> None:
        """Set default queue for unmatched items."""
        self._default_queue = queue_name
    
    async def route(self, item: Any) -> bool:
        """Route item to appropriate queue."""
        # Check routes in order
        for predicate, queue_name in self._routes:
            try:
                if predicate(item):
                    queue = self.queue_manager.get_queue(queue_name)
                    if queue:
                        return await queue.enqueue(item)
            except Exception as e:
                logger.error(
                    "route_predicate_error",
                    error=str(e)
                )
        
        # Use default queue
        if self._default_queue:
            queue = self.queue_manager.get_queue(self._default_queue)
            if queue:
                return await queue.enqueue(item)
        
        return False