"""Enhanced metrics collection from paste-3.txt with additions."""

from enum import Flag, auto
from functools import wraps
import logging
from typing import Optional, Dict, Any, List, Set, Callable
import time
import traceback
import json
from collections import defaultdict
import asyncio
from dataclasses import dataclass, field
import statistics
from datetime import datetime
import threading
from prometheus_client import Counter, Histogram, Gauge, Summary, CollectorRegistry


class MetricType(Flag):
    """Enhanced metric types for monitoring"""
    NONE = 0
    TIMING = auto()  # Execution timing and durations
    RESOURCES = auto()  # Resource usage and allocation
    DEPENDENCIES = auto()  # Dependency resolution and state
    STATE_CHANGES = auto()  # State transitions and lifecycle
    ERRORS = auto()  # Error tracking and handling
    THROUGHPUT = auto()  # Operations per second
    QUEUE_STATS = auto()  # Queue statistics
    CONCURRENCY = auto()  # Concurrency levels
    MEMORY = auto()  # Memory usage
    PERIODIC = auto()  # Periodic task stats
    RETRIES = auto()  # Retry statistics
    LATENCY = auto()  # Operation latency
    ALL = (TIMING | RESOURCES | DEPENDENCIES | STATE_CHANGES |
           ERRORS | THROUGHPUT | QUEUE_STATS | CONCURRENCY |
           MEMORY | PERIODIC | RETRIES | LATENCY)


@dataclass
class MetricAggregation:
    """Aggregated metrics container"""
    count: int = 0
    sum: float = 0.0
    min: float = float('inf')
    max: float = float('-inf')
    values: List[float] = field(default_factory=list)

    def add(self, value: float) -> None:
        self.count += 1
        self.sum += value
        self.min = min(self.min, value)
        self.max = max(self.max, value)
        self.values.append(value)

    @property
    def avg(self) -> float:
        return self.sum / self.count if self.count > 0 else 0

    @property
    def median(self) -> float:
        return statistics.median(self.values) if self.values else 0

    @property
    def percentile_95(self) -> float:
        return statistics.quantiles(self.values, n=20)[-1] if len(self.values) >= 20 else self.max

    def to_dict(self) -> dict:
        return {
            "count": self.count,
            "min": self.min,
            "max": self.max,
            "avg": self.avg,
            "median": self.median,
            "p95": self.percentile_95
        }


class RealTimeMetrics:
    """Real-time metric collection and broadcasting"""

    def __init__(self, update_interval: float = 1.0):
        self.metrics: Dict[str, Any] = defaultdict(lambda: defaultdict(MetricAggregation))
        self.subscribers: Set[callable] = set()
        self.update_interval = update_interval
        self._update_task: Optional[asyncio.Task] = None
        self._lock = threading.Lock()

    def subscribe(self, callback: callable) -> None:
        """Subscribe to real-time metric updates"""
        self.subscribers.add(callback)

    def unsubscribe(self, callback: callable) -> None:
        """Unsubscribe from real-time metric updates"""
        self.subscribers.discard(callback)

    async def _broadcast_updates(self) -> None:
        """Broadcast metric updates to subscribers"""
        while True:
            with self._lock:
                metrics_snapshot = {
                    category: {
                        metric: agg.to_dict()
                        for metric, agg in metrics.items()
                    }
                    for category, metrics in self.metrics.items()
                }

            for subscriber in self.subscribers:
                try:
                    if asyncio.iscoroutinefunction(subscriber):
                        await subscriber(metrics_snapshot)
                    else:
                        subscriber(metrics_snapshot)
                except Exception:
                    pass

            await asyncio.sleep(self.update_interval)

    def start(self) -> None:
        """Start real-time metric broadcasting"""
        if not self._update_task:
            self._update_task = asyncio.create_task(self._broadcast_updates())

    def stop(self) -> None:
        """Stop real-time metric broadcasting"""
        if self._update_task:
            self._update_task.cancel()
            self._update_task = None

    def record(self, category: str, name: str, value: float) -> None:
        """Record a metric value"""
        with self._lock:
            self.metrics[category][name].add(value)


class MetricsCollector:
    """Central metrics collector with Prometheus export."""
    
    def __init__(self, registry: Optional[CollectorRegistry] = None):
        self.registry = registry or CollectorRegistry()
        self._metrics: Dict[str, Any] = {}
        self._lock = threading.Lock()
        self._setup_prometheus_metrics()
    
    def _setup_prometheus_metrics(self):
        """Setup Prometheus metrics."""
        # State metrics
        self._metrics['state_duration'] = Histogram(
            'workflow_state_duration_seconds',
            'State execution duration in seconds',
            ['agent', 'state', 'status'],
            registry=self.registry
        )
        
        self._metrics['state_total'] = Counter(
            'workflow_state_total',
            'Total number of state executions',
            ['agent', 'state', 'status'],
            registry=self.registry
        )
        
        # Resource metrics
        self._metrics['resource_usage'] = Gauge(
            'workflow_resource_usage',
            'Current resource usage',
            ['resource_type', 'agent'],
            registry=self.registry
        )
        
        # Queue metrics
        self._metrics['queue_size'] = Gauge(
            'workflow_queue_size',
            'Current queue size',
            ['agent', 'priority'],
            registry=self.registry
        )
        
        # Error metrics
        self._metrics['errors_total'] = Counter(
            'workflow_errors_total',
            'Total number of errors',
            ['agent', 'state', 'error_type'],
            registry=self.registry
        )
        
        # Agent metrics
        self._metrics['agent_status'] = Gauge(
            'workflow_agent_status',
            'Current agent status (0=idle, 1=running, 2=paused, 3=completed, 4=failed)',
            ['agent'],
            registry=self.registry
        )
    
    def record_state_execution(
        self,
        agent: str,
        state: str,
        duration: float,
        status: str
    ):
        """Record state execution metrics."""
        self._metrics['state_duration'].labels(
            agent=agent,
            state=state,
            status=status
        ).observe(duration)
        
        self._metrics['state_total'].labels(
            agent=agent,
            state=state,
            status=status
        ).inc()
    
    def record_resource_usage(
        self,
        resource_type: str,
        agent: str,
        usage: float
    ):
        """Record resource usage metrics."""
        self._metrics['resource_usage'].labels(
            resource_type=resource_type,
            agent=agent
        ).set(usage)
    
    def record_queue_size(self, agent: str, priority: str, size: int):
        """Record queue size metrics."""
        self._metrics['queue_size'].labels(
            agent=agent,
            priority=priority
        ).set(size)
    
    def record_error(self, agent: str, state: str, error_type: str):
        """Record error metrics."""
        self._metrics['errors_total'].labels(
            agent=agent,
            state=state,
            error_type=error_type
        ).inc()
    
    def set_agent_status(self, agent: str, status: int):
        """Set agent status metric."""
        self._metrics['agent_status'].labels(agent=agent).set(status)


class PrometheusExporter:
    """Prometheus metrics exporter."""
    
    def __init__(self, port: int = 9090):
        from prometheus_client import start_http_server
        self.port = port
        self._server = None
    
    def start(self):
        """Start Prometheus HTTP server."""
        from prometheus_client import start_http_server
        self._server = start_http_server(self.port)
    
    def stop(self):
        """Stop Prometheus HTTP server."""
        if self._server:
            self._server.shutdown()


# Agent monitor decorator from paste-3.txt
def agent_monitor(
        metrics: MetricType = MetricType.ALL,
        log_level: int = logging.INFO,
        metrics_callback: Optional[callable] = None,
        realtime: bool = False,
        update_interval: float = 1.0,
        aggregate: bool = True,
        collector: Optional[MetricsCollector] = None
):
    """
    Enhanced decorator for comprehensive agent monitoring
    """

    def decorator(coro):
        @wraps(coro)
        async def wrapper(*args, **kwargs):
            agent = args[0] if len(args) > 0 else kwargs.get('agent')
            if not agent:
                return await coro(*args, **kwargs)

            # Initialize monitoring
            start_time = time.time()
            rt_metrics = RealTimeMetrics(update_interval) if realtime else None
            collected_metrics = defaultdict(lambda: defaultdict(MetricAggregation))
            
            # Use provided collector or create new one
            metrics_collector = collector or MetricsCollector()

            logger = logging.getLogger(f"Agent_{agent.name}")
            logger.setLevel(log_level)

            if realtime:
                rt_metrics.start()
                if metrics_callback:
                    rt_metrics.subscribe(metrics_callback)

            def update_metric(category: str, name: str, value: float) -> None:
                """Update both real-time and collected metrics"""
                collected_metrics[category][name].add(value)
                if rt_metrics:
                    rt_metrics.record(category, name, value)

            # Enhanced metric collection methods
            original_methods = {}

            if MetricType.STATE_CHANGES in metrics:
                original_methods['run_state'] = agent.run_state

                async def monitored_run_state(state_name, *args, **kwargs):
                    state_start = time.time()
                    try:
                        result = await original_methods['run_state'](state_name, *args, **kwargs)
                        duration = time.time() - state_start
                        update_metric("states", state_name, duration)
                        update_metric("states", "total_execution", duration)
                        
                        # Record to Prometheus
                        metrics_collector.record_state_execution(
                            agent.name,
                            state_name,
                            duration,
                            "success"
                        )
                        
                        return result
                    except Exception as e:
                        duration = time.time() - state_start
                        update_metric("errors", state_name, duration)
                        
                        # Record to Prometheus
                        metrics_collector.record_state_execution(
                            agent.name,
                            state_name,
                            duration,
                            "error"
                        )
                        metrics_collector.record_error(
                            agent.name,
                            state_name,
                            type(e).__name__
                        )
                        raise

                agent.run_state = monitored_run_state

            if MetricType.QUEUE_STATS in metrics:
                original_methods['_add_to_queue'] = agent._add_to_queue

                def monitored_add_to_queue(state_name, metadata, priority_boost=0):
                    queue_size = len(agent.priority_queue)
                    update_metric("queue", "size", queue_size)
                    update_metric("queue", "priority", priority_boost)
                    
                    # Record to Prometheus
                    metrics_collector.record_queue_size(
                        agent.name,
                        str(metadata.resources.priority),
                        queue_size
                    )
                    
                    return original_methods['_add_to_queue'](state_name, metadata, priority_boost)

                agent._add_to_queue = monitored_add_to_queue

            if MetricType.DEPENDENCIES in metrics:
                original_methods['_resolve_dependencies'] = agent._resolve_dependencies

                async def monitored_resolve_dependencies(state_name):
                    start_time = time.time()
                    try:
                        result = await original_methods['_resolve_dependencies'](state_name)
                        duration = time.time() - start_time
                        update_metric("dependencies", f"resolve_{state_name}", duration)
                        return result
                    except Exception as e:
                        update_metric("errors", f"dep_resolve_{state_name}", time.time() - start_time)
                        raise

                agent._resolve_dependencies = monitored_resolve_dependencies

            try:
                # Set agent status
                metrics_collector.set_agent_status(agent.name, 1)  # Running
                
                # Execute agent
                logger.info(f"Starting agent execution: {agent.name}")
                result = await coro(*args, **kwargs)

                # Collect final metrics
                execution_time = time.time() - start_time
                update_metric("timing", "total_execution", execution_time)

                if MetricType.RESOURCES in metrics:
                    for rtype, available in agent.resource_pool.available.items():
                        update_metric("resources", f"available_{rtype.name}", available)
                        used = agent.resource_pool.resources[rtype] - available
                        update_metric("resources", f"used_{rtype.name}", used)
                        
                        # Record to Prometheus
                        metrics_collector.record_resource_usage(
                            rtype.name,
                            agent.name,
                            used
                        )

                if MetricType.CONCURRENCY in metrics:
                    update_metric("concurrency", "max_concurrent", len(agent._running_states))

                if MetricType.THROUGHPUT in metrics:
                    states_per_second = len(agent.completed_states) / execution_time
                    update_metric("throughput", "states_per_second", states_per_second)

                # Set agent status to completed
                metrics_collector.set_agent_status(agent.name, 3)  # Completed
                
                logger.info(
                    f"Agent execution completed in {execution_time * 1000:.2f}ms"
                )

                return result

            except Exception as e:
                # Set agent status to failed
                metrics_collector.set_agent_status(agent.name, 4)  # Failed
                
                if MetricType.ERRORS in metrics:
                    update_metric("errors", "count", 1)
                    update_metric("errors", str(e), 1)
                logger.error(f"Agent execution failed: {e}")
                raise

            finally:
                # Stop real-time updates
                if rt_metrics:
                    rt_metrics.stop()

                # Restore original methods
                for name, method in original_methods.items():
                    setattr(agent, name, method)

                # Format final metrics
                final_metrics = {
                    category: {
                        metric: agg.to_dict() if aggregate else agg.values
                        for metric, agg in metrics.items()
                    }
                    for category, metrics in collected_metrics.items()
                }

                if metrics_callback and not realtime:
                    if asyncio.iscoroutinefunction(metrics_callback):
                        await metrics_callback(final_metrics)
                    else:
                        metrics_callback(final_metrics)

        return wrapper

    return decorator