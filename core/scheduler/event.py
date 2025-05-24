"""Event-based scheduling and triggers."""

import asyncio
from typing import Dict, List, Optional, Any, Callable, Union, Pattern
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import re
import structlog
from pathlib import Path
import aiofiles
import watchdog.observers
import watchdog.events
from abc import ABC, abstractmethod
import json
import fnmatch

from core.scheduler.base import Scheduler, Schedule, ScheduleType, ScheduledTask


logger = structlog.get_logger(__name__)


class EventType(Enum):
    """Types of events that can trigger schedules."""
    FILE_CREATED = "file_created"
    FILE_MODIFIED = "file_modified"
    FILE_DELETED = "file_deleted"
    FILE_MOVED = "file_moved"
    WEBHOOK = "webhook"
    MESSAGE = "message"
    TIMER = "timer"
    SIGNAL = "signal"
    CUSTOM = "custom"


@dataclass
class EventPattern:
    """Pattern for matching events."""
    event_type: EventType
    pattern: Optional[str] = None  # Regex or glob pattern
    filters: Dict[str, Any] = field(default_factory=dict)
    
    def matches(self, event: Dict[str, Any]) -> bool:
        """Check if event matches pattern."""
        if event.get("type") != self.event_type.value:
            return False
        
        # Check pattern
        if self.pattern:
            target = event.get("target", "")
            if not self._match_pattern(target, self.pattern):
                return False
        
        # Check filters
        for key, value in self.filters.items():
            if key not in event:
                return False
            
            event_value = event[key]
            
            # Support different filter types
            if isinstance(value, dict):
                # Range filter {"min": x, "max": y}
                if "min" in value and event_value < value["min"]:
                    return False
                if "max" in value and event_value > value["max"]:
                    return False
            elif isinstance(value, list):
                # In filter
                if event_value not in value:
                    return False
            else:
                # Exact match
                if event_value != value:
                    return False
        
        return True
    
    def _match_pattern(self, text: str, pattern: str) -> bool:
        """Match text against pattern (glob or regex)."""
        # Try glob first
        if fnmatch.fnmatch(text, pattern):
            return True
        
        # Try regex
        try:
            return bool(re.match(pattern, text))
        except re.error:
            return False


class EventTrigger(ABC):
    """Base class for event triggers."""
    
    def __init__(self, name: str):
        self.name = name
        self._callbacks: List[Callable[[Dict[str, Any]], None]] = []
        self._running = False
    
    def add_callback(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """Add callback for events."""
        self._callbacks.append(callback)
    
    def remove_callback(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """Remove callback."""
        if callback in self._callbacks:
            self._callbacks.remove(callback)
    
    async def emit_event(self, event: Dict[str, Any]) -> None:
        """Emit event to all callbacks."""
        event["timestamp"] = datetime.utcnow().isoformat()
        event["trigger"] = self.name
        
        for callback in self._callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(event)
                else:
                    await asyncio.get_event_loop().run_in_executor(
                        None, callback, event
                    )
            except Exception as e:
                logger.error(
                    "event_callback_error",
                    trigger=self.name,
                    error=str(e)
                )
    
    @abstractmethod
    async def start(self) -> None:
        """Start the trigger."""
        pass
    
    @abstractmethod
    async def stop(self) -> None:
        """Stop the trigger."""
        pass


class FileWatcher(EventTrigger):
    """Watch filesystem for changes."""
    
    def __init__(
        self,
        name: str,
        path: Union[str, Path],
        patterns: Optional[List[str]] = None,
        recursive: bool = True
    ):
        super().__init__(name)
        self.path = Path(path)
        self.patterns = patterns or ["*"]
        self.recursive = recursive
        self._observer = None
        self._handler = None
    
    async def start(self) -> None:
        """Start watching."""
        self._running = True
        
        # Create event handler
        self._handler = FileEventHandler(self, self.patterns)
        
        # Create observer
        self._observer = watchdog.observers.Observer()
        self._observer.schedule(
            self._handler,
            str(self.path),
            recursive=self.recursive
        )
        
        # Start observer in thread
        self._observer.start()
        
        logger.info(
            "file_watcher_started",
            name=self.name,
            path=str(self.path),
            patterns=self.patterns
        )
    
    async def stop(self) -> None:
        """Stop watching."""
        self._running = False
        
        if self._observer:
            self._observer.stop()
            self._observer.join()
            self._observer = None
        
        logger.info("file_watcher_stopped", name=self.name)


class FileEventHandler(watchdog.events.FileSystemEventHandler):
    """Handle filesystem events."""
    
    def __init__(self, watcher: FileWatcher, patterns: List[str]):
        self.watcher = watcher
        self.patterns = patterns
    
    def _matches_pattern(self, path: str) -> bool:
        """Check if path matches any pattern."""
        for pattern in self.patterns:
            if fnmatch.fnmatch(path, pattern):
                return True
        return False
    
    def on_created(self, event):
        """File created."""
        if not event.is_directory and self._matches_pattern(event.src_path):
            asyncio.create_task(self.watcher.emit_event({
                "type": EventType.FILE_CREATED.value,
                "target": event.src_path,
                "is_directory": event.is_directory
            }))
    
    def on_modified(self, event):
        """File modified."""
        if not event.is_directory and self._matches_pattern(event.src_path):
            asyncio.create_task(self.watcher.emit_event({
                "type": EventType.FILE_MODIFIED.value,
                "target": event.src_path,
                "is_directory": event.is_directory
            }))
    
    def on_deleted(self, event):
        """File deleted."""
        if self._matches_pattern(event.src_path):
            asyncio.create_task(self.watcher.emit_event({
                "type": EventType.FILE_DELETED.value,
                "target": event.src_path,
                "is_directory": event.is_directory
            }))
    
    def on_moved(self, event):
        """File moved."""
        if self._matches_pattern(event.src_path) or self._matches_pattern(event.dest_path):
            asyncio.create_task(self.watcher.emit_event({
                "type": EventType.FILE_MOVED.value,
                "target": event.src_path,
                "destination": event.dest_path,
                "is_directory": event.is_directory
            }))


class WebhookTrigger(EventTrigger):
    """Trigger events from webhooks."""
    
    def __init__(
        self,
        name: str,
        port: int = 8080,
        path: str = "/webhook",
        auth_token: Optional[str] = None
    ):
        super().__init__(name)
        self.port = port
        self.path = path
        self.auth_token = auth_token
        self._server = None
        self._site = None
    
    async def start(self) -> None:
        """Start webhook server."""
        from aiohttp import web
        
        self._running = True
        
        # Create app
        app = web.Application()
        app.router.add_post(self.path, self._handle_webhook)
        
        # Start server
        runner = web.AppRunner(app)
        await runner.setup()
        self._site = web.TCPSite(runner, '0.0.0.0', self.port)
        await self._site.start()
        
        logger.info(
            "webhook_trigger_started",
            name=self.name,
            port=self.port,
            path=self.path
        )
    
    async def stop(self) -> None:
        """Stop webhook server."""
        self._running = False
        
        if self._site:
            await self._site.stop()
            self._site = None
        
        logger.info("webhook_trigger_stopped", name=self.name)
    
    async def _handle_webhook(self, request):
        """Handle incoming webhook."""
        from aiohttp import web
        
        # Check auth
        if self.auth_token:
            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith(f"Bearer {self.auth_token}"):
                return web.Response(status=401)
        
        try:
            # Parse body
            body = await request.json()
            
            # Emit event
            await self.emit_event({
                "type": EventType.WEBHOOK.value,
                "target": request.path,
                "method": request.method,
                "headers": dict(request.headers),
                "body": body
            })
            
            return web.Response(status=200, text="OK")
            
        except Exception as e:
            logger.error(
                "webhook_handler_error",
                error=str(e)
            )
            return web.Response(status=500, text=str(e))


class MessageQueueTrigger(EventTrigger):
    """Trigger events from message queues."""
    
    def __init__(
        self,
        name: str,
        queue_url: str,
        queue_type: str = "redis",  # redis, rabbitmq, kafka, etc.
        **kwargs
    ):
        super().__init__(name)
        self.queue_url = queue_url
        self.queue_type = queue_type
        self.config = kwargs
        self._consumer_task = None
    
    async def start(self) -> None:
        """Start consuming messages."""
        self._running = True
        self._consumer_task = asyncio.create_task(self._consume_messages())
        
        logger.info(
            "message_queue_trigger_started",
            name=self.name,
            queue_type=self.queue_type
        )
    
    async def stop(self) -> None:
        """Stop consuming messages."""
        self._running = False
        
        if self._consumer_task:
            self._consumer_task.cancel()
            await asyncio.gather(self._consumer_task, return_exceptions=True)
            self._consumer_task = None
        
        logger.info("message_queue_trigger_stopped", name=self.name)
    
    async def _consume_messages(self) -> None:
        """Consume messages from queue."""
        # This is a placeholder - actual implementation would depend on queue type
        while self._running:
            try:
                # Simulate message consumption
                await asyncio.sleep(1)
                
                # In real implementation, would:
                # 1. Connect to queue
                # 2. Subscribe/consume messages
                # 3. Emit events for each message
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(
                    "message_consumer_error",
                    error=str(e)
                )
                await asyncio.sleep(5)  # Backoff on error


class EventScheduler(Scheduler):
    """Scheduler that responds to events."""
    
    def __init__(self, workflow_engine=None, store=None):
        super().__init__(workflow_engine, store)
        self._triggers: Dict[str, EventTrigger] = {}
        self._event_schedules: Dict[str, List[str]] = {}  # event_pattern -> schedule_ids
    
    async def start(self) -> None:
        """Start scheduler and triggers."""
        await super().start()
        
        # Start all triggers
        for trigger in self._triggers.values():
            await trigger.start()
    
    async def stop(self) -> None:
        """Stop scheduler and triggers."""
        # Stop all triggers
        for trigger in self._triggers.values():
            await trigger.stop()
        
        await super().stop()
    
    def add_trigger(self, trigger: EventTrigger) -> None:
        """Add an event trigger."""
        self._triggers[trigger.name] = trigger
        
        # Set up callback
        trigger.add_callback(self._handle_event)
        
        logger.info("trigger_added", name=trigger.name)
    
    def remove_trigger(self, name: str) -> None:
        """Remove an event trigger."""
        if name in self._triggers:
            trigger = self._triggers.pop(name)
            trigger.remove_callback(self._handle_event)
            
            logger.info("trigger_removed", name=name)
    
    async def add_event_schedule(
        self,
        name: str,
        event_pattern: EventPattern,
        workflow_id: Optional[str] = None,
        agent_config: Optional[Dict[str, Any]] = None,
        **kwargs
    ) -> Schedule:
        """Add an event-triggered schedule."""
        import uuid
        
        schedule = Schedule(
            schedule_id=str(uuid.uuid4()),
            name=name,
            type=ScheduleType.EVENT,
            config={
                "event_type": event_pattern.event_type.value,
                "pattern": event_pattern.pattern,
                "filters": event_pattern.filters
            },
            workflow_id=workflow_id,
            agent_config=agent_config,
            **kwargs
        )
        
        await self.add_schedule(schedule)
        
        # Index by event pattern
        pattern_key = f"{event_pattern.event_type.value}:{event_pattern.pattern}"
        if pattern_key not in self._event_schedules:
            self._event_schedules[pattern_key] = []
        self._event_schedules[pattern_key].append(schedule.schedule_id)
        
        return schedule
    
    async def _handle_event(self, event: Dict[str, Any]) -> None:
        """Handle incoming event."""
        logger.debug("event_received", event=event)
        
        # Find matching schedules
        for schedule in self._schedules.values():
            if schedule.type != ScheduleType.EVENT or not schedule.enabled:
                continue
            
            # Check if event matches schedule pattern
            pattern = EventPattern(
                event_type=EventType(schedule.config.get("event_type")),
                pattern=schedule.config.get("pattern"),
                filters=schedule.config.get("filters", {})
            )
            
            if pattern.matches(event):
                # Create task
                task = ScheduledTask(
                    task_id=str(uuid.uuid4()),
                    schedule_id=schedule.schedule_id,
                    scheduled_time=datetime.utcnow(),
                    workflow_id=schedule.workflow_id,
                    agent_config=schedule.agent_config
                )
                
                # Add event data to task
                task.metadata = {"event": event}
                
                # Execute immediately
                asyncio.create_task(self._execute_task(task))
                
                logger.info(
                    "event_triggered_schedule",
                    schedule_id=schedule.schedule_id,
                    event_type=event.get("type"),
                    task_id=task.task_id
                )
    
    async def _calculate_next_run(self, schedule: Schedule) -> Optional[datetime]:
        """Calculate next run for event schedule (not applicable)."""
        if schedule.type == ScheduleType.EVENT:
            # Event schedules don't have predetermined run times
            return None
        
        return await super()._calculate_next_run(schedule)


# Predefined event patterns
class EventPatterns:
    """Common event patterns."""
    
    # File patterns
    CSV_CREATED = EventPattern(
        EventType.FILE_CREATED,
        pattern="*.csv"
    )
    
    JSON_MODIFIED = EventPattern(
        EventType.FILE_MODIFIED,
        pattern="*.json"
    )
    
    LOG_ROTATED = EventPattern(
        EventType.FILE_MOVED,
        pattern="*.log"
    )
    
    # Webhook patterns
    GITHUB_PUSH = EventPattern(
        EventType.WEBHOOK,
        filters={"headers.X-GitHub-Event": "push"}
    )
    
    STRIPE_PAYMENT = EventPattern(
        EventType.WEBHOOK,
        filters={"body.type": "payment_intent.succeeded"}
    )
    
    # Custom patterns
    @staticmethod
    def file_size_threshold(size_mb: float) -> EventPattern:
        """Trigger when file exceeds size."""
        return EventPattern(
            EventType.FILE_CREATED,
            filters={"size": {"min": size_mb * 1024 * 1024}}
        )
    
    @staticmethod
    def file_in_directory(directory: str, pattern: str = "*") -> EventPattern:
        """Trigger for files in specific directory."""
        return EventPattern(
            EventType.FILE_CREATED,
            pattern=f"{directory}/{pattern}"
        )


import uuid