"""OpenTelemetry integration for distributed tracing."""

import asyncio
import contextlib
from typing import Optional, Dict, Any, Callable, TypeVar, ParamSpec
from functools import wraps
from enum import Enum
import structlog

from opentelemetry import trace, metrics, baggage
from opentelemetry.trace import Tracer, Span, Status, StatusCode
from opentelemetry.metrics import Meter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.instrumentation.asyncio import AsyncioInstrumentor

from core.config import get_settings


logger = structlog.get_logger(__name__)
P = ParamSpec('P')
T = TypeVar('T')


class SpanKind(Enum):
    """Types of spans for tracing."""
    INTERNAL = "internal"
    SERVER = "server"
    CLIENT = "client"
    PRODUCER = "producer"
    CONSUMER = "consumer"


class TracingManager:
    """Manages OpenTelemetry tracing for the workflow orchestrator."""
    
    def __init__(self, service_name: str = "workflow-orchestrator"):
        self.settings = get_settings()
        self.service_name = service_name
        self._tracer_provider: Optional[TracerProvider] = None
        self._meter_provider: Optional[MeterProvider] = None
        self._tracer: Optional[Tracer] = None
        self._meter: Optional[Meter] = None
        self._propagator = TraceContextTextMapPropagator()
        
        self._initialize()
    
    def _initialize(self):
        """Initialize OpenTelemetry providers."""
        # Create resource
        resource = Resource.create({
            SERVICE_NAME: self.service_name,
            "service.version": "0.1.0",
            "deployment.environment": self.settings.environment,
        })
        
        # Setup tracing
        self._tracer_provider = TracerProvider(resource=resource)
        
        # Add span processors
        if self.settings.otlp_endpoint:
            otlp_exporter = OTLPSpanExporter(
                endpoint=self.settings.otlp_endpoint,
                insecure=True  # Use secure=False for development
            )
            span_processor = BatchSpanProcessor(otlp_exporter)
            self._tracer_provider.add_span_processor(span_processor)
        
        if self.settings.debug:
            # Also export to console in debug mode
            console_exporter = ConsoleSpanExporter()
            console_processor = BatchSpanProcessor(console_exporter)
            self._tracer_provider.add_span_processor(console_processor)
        
        trace.set_tracer_provider(self._tracer_provider)
        self._tracer = trace.get_tracer(__name__)
        
        # Setup metrics
        if self.settings.enable_metrics:
            self._meter_provider = MeterProvider(resource=resource)
            
            if self.settings.otlp_endpoint:
                metric_exporter = OTLPMetricExporter(
                    endpoint=self.settings.otlp_endpoint,
                    insecure=True
                )
                metric_reader = PeriodicExportingMetricReader(
                    exporter=metric_exporter,
                    export_interval_millis=10000  # 10 seconds
                )
                self._meter_provider._sdk_config.metric_readers.append(metric_reader)
            
            metrics.set_meter_provider(self._meter_provider)
            self._meter = metrics.get_meter(__name__)
        
        # Instrument asyncio
        AsyncioInstrumentor().instrument()
        
        logger.info(
            "telemetry_initialized",
            service_name=self.service_name,
            otlp_endpoint=self.settings.otlp_endpoint
        )
    
    def get_tracer(self) -> Tracer:
        """Get the configured tracer."""
        return self._tracer
    
    def get_meter(self) -> Meter:
        """Get the configured meter."""
        return self._meter
    
    @contextlib.contextmanager
    def trace_operation(
        self,
        name: str,
        attributes: Optional[Dict[str, Any]] = None,
        kind: SpanKind = SpanKind.INTERNAL
    ):
        """Create a traced operation context."""
        otel_kind = getattr(trace.SpanKind, kind.value.upper())
        
        with self._tracer.start_as_current_span(
            name,
            kind=otel_kind,
            attributes=attributes or {}
        ) as span:
            try:
                yield span
            except Exception as e:
                span.set_status(
                    Status(StatusCode.ERROR, str(e))
                )
                span.record_exception(e)
                raise
    
    @contextlib.contextmanager
    def trace_workflow(self, workflow_id: str):
        """Create a workflow execution trace."""
        with self.trace_operation(
            "workflow.execute",
            attributes={
                "workflow.id": workflow_id,
                "workflow.service": self.service_name
            },
            kind=SpanKind.SERVER
        ) as span:
            # Set workflow ID in baggage for propagation
            baggage.set_baggage("workflow_id", workflow_id)
            yield span
    
    @contextlib.contextmanager
    def trace_state(
        self,
        agent_name: str,
        state_name: str,
        attempt: int = 1
    ):
        """Create a state execution trace."""
        with self.trace_operation(
            f"state.{state_name}",
            attributes={
                "agent.name": agent_name,
                "state.name": state_name,
                "state.attempt": attempt,
                "workflow.id": baggage.get_baggage("workflow_id")
            },
            kind=SpanKind.INTERNAL
        ) as span:
            yield span
    
    def inject_context(self, carrier: Dict[str, str]) -> None:
        """Inject trace context into carrier for propagation."""
        self._propagator.inject(carrier)
    
    def extract_context(self, carrier: Dict[str, str]) -> None:
        """Extract trace context from carrier."""
        ctx = self._propagator.extract(carrier)
        trace.set_span_in_context(trace.get_current_span(), ctx)
    
    def create_counter(
        self,
        name: str,
        description: str,
        unit: str = "1"
    ) -> metrics.Counter:
        """Create a counter metric."""
        return self._meter.create_counter(
            name=name,
            description=description,
            unit=unit
        )
    
    def create_histogram(
        self,
        name: str,
        description: str,
        unit: str = "ms"
    ) -> metrics.Histogram:
        """Create a histogram metric."""
        return self._meter.create_histogram(
            name=name,
            description=description,
            unit=unit
        )
    
    def create_gauge(
        self,
        name: str,
        description: str,
        unit: str = "1"
    ) -> metrics.ObservableGauge:
        """Create a gauge metric."""
        return self._meter.create_observable_gauge(
            name=name,
            description=description,
            unit=unit
        )


# Global tracing manager instance
_tracing_manager: Optional[TracingManager] = None


def get_tracer() -> Tracer:
    """Get the global tracer instance."""
    global _tracing_manager
    if not _tracing_manager:
        _tracing_manager = TracingManager()
    return _tracing_manager.get_tracer()


def trace_operation(
    name: Optional[str] = None,
    kind: SpanKind = SpanKind.INTERNAL,
    attributes: Optional[Dict[str, Any]] = None
) -> Callable:
    """Decorator for tracing operations."""
    def decorator(func: Callable[P, T]) -> Callable[P, T]:
        operation_name = name or f"{func.__module__}.{func.__name__}"
        
        if asyncio.iscoroutinefunction(func):
            @wraps(func)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
                tracer = get_tracer()
                with tracer.start_as_current_span(
                    operation_name,
                    kind=getattr(trace.SpanKind, kind.value.upper()),
                    attributes=attributes or {}
                ) as span:
                    try:
                        # Add function arguments as span attributes
                        if args:
                            span.set_attribute("args.count", len(args))
                        if kwargs:
                            span.set_attribute("kwargs.keys", list(kwargs.keys()))
                        
                        result = await func(*args, **kwargs)
                        span.set_status(Status(StatusCode.OK))
                        return result
                    except Exception as e:
                        span.set_status(
                            Status(StatusCode.ERROR, str(e))
                        )
                        span.record_exception(e)
                        raise
            return async_wrapper
        else:
            @wraps(func)
            def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
                tracer = get_tracer()
                with tracer.start_as_current_span(
                    operation_name,
                    kind=getattr(trace.SpanKind, kind.value.upper()),
                    attributes=attributes or {}
                ) as span:
                    try:
                        # Add function arguments as span attributes
                        if args:
                            span.set_attribute("args.count", len(args))
                        if kwargs:
                            span.set_attribute("kwargs.keys", list(kwargs.keys()))
                        
                        result = func(*args, **kwargs)
                        span.set_status(Status(StatusCode.OK))
                        return result
                    except Exception as e:
                        span.set_status(
                            Status(StatusCode.ERROR, str(e))
                        )
                        span.record_exception(e)
                        raise
            return sync_wrapper
    return decorator