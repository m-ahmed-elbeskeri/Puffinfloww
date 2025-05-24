"""Alert DSL and management system."""

import asyncio
from typing import Dict, List, Tuple, Optional, Any, Callable, Union, Set
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
import json
import re
from abc import ABC, abstractmethod
import structlog
import httpx
import smtplib
from email.message import EmailMessage

from core.monitoring.metrics import MetricAggregation
from core.storage.events import WorkflowEvent, EventType


logger = structlog.get_logger(__name__)


class AlertSeverity(Enum):
    """Alert severity levels."""
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class AlertState(Enum):
    """Alert states."""
    PENDING = "pending"
    FIRING = "firing"
    RESOLVED = "resolved"


@dataclass
class AlertCondition:
    """Condition for triggering alerts."""
    metric: str
    operator: str  # >, <, >=, <=, ==, !=
    threshold: float
    window: timedelta
    aggregation: str = "avg"  # avg, sum, min, max, count
    
    def evaluate(self, values: List[float]) -> bool:
        """Evaluate condition against values."""
        if not values:
            return False
        
        # Aggregate values
        if self.aggregation == "avg":
            value = sum(values) / len(values)
        elif self.aggregation == "sum":
            value = sum(values)
        elif self.aggregation == "min":
            value = min(values)
        elif self.aggregation == "max":
            value = max(values)
        elif self.aggregation == "count":
            value = len(values)
        else:
            value = values[-1]  # Latest value
        
        # Evaluate operator
        if self.operator == ">":
            return value > self.threshold
        elif self.operator == "<":
            return value < self.threshold
        elif self.operator == ">=":
            return value >= self.threshold
        elif self.operator == "<=":
            return value <= self.threshold
        elif self.operator == "==":
            return value == self.threshold
        elif self.operator == "!=":
            return value != self.threshold
        else:
            return False


@dataclass
class Alert:
    """Alert instance."""
    alert_id: str
    rule_name: str
    severity: AlertSeverity
    state: AlertState
    message: str
    labels: Dict[str, str]
    annotations: Dict[str, str]
    fired_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    value: Optional[float] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert alert to dictionary."""
        return {
            "alert_id": self.alert_id,
            "rule_name": self.rule_name,
            "severity": self.severity.value,
            "state": self.state.value,
            "message": self.message,
            "labels": self.labels,
            "annotations": self.annotations,
            "fired_at": self.fired_at.isoformat() if self.fired_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "value": self.value
        }


class AlertDestination(ABC):
    """Base class for alert destinations."""
    
    @abstractmethod
    async def send(self, alert: Alert) -> None:
        """Send alert to destination."""
        pass


class WebhookDestination(AlertDestination):
    """Send alerts to webhook endpoints."""
    
    def __init__(self, url: str, headers: Optional[Dict[str, str]] = None):
        self.url = url
        self.headers = headers or {}
    
    async def send(self, alert: Alert) -> None:
        """Send alert via webhook."""
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    self.url,
                    json=alert.to_dict(),
                    headers=self.headers,
                    timeout=30.0
                )
                response.raise_for_status()
                
                logger.info(
                    "alert_sent_webhook",
                    alert_id=alert.alert_id,
                    url=self.url,
                    status=response.status_code
                )
                
            except Exception as e:
                logger.error(
                    "alert_webhook_failed",
                    alert_id=alert.alert_id,
                    url=self.url,
                    error=str(e)
                )


class EmailDestination(AlertDestination):
    """Send alerts via email."""
    
    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        username: str,
        password: str,
        from_addr: str,
        to_addrs: List[str],
        use_tls: bool = True
    ):
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.username = username
        self.password = password
        self.from_addr = from_addr
        self.to_addrs = to_addrs
        self.use_tls = use_tls
    
    async def send(self, alert: Alert) -> None:
        """Send alert via email."""
        msg = EmailMessage()
        msg["Subject"] = f"[{alert.severity.value.upper()}] {alert.rule_name}"
        msg["From"] = self.from_addr
        msg["To"] = ", ".join(self.to_addrs)
        
        # Create email body
        body = f"""
Alert: {alert.rule_name}
Severity: {alert.severity.value}
State: {alert.state.value}

Message: {alert.message}

Labels:
{json.dumps(alert.labels, indent=2)}

Annotations:
{json.dumps(alert.annotations, indent=2)}

Fired at: {alert.fired_at}
Value: {alert.value}
"""
        msg.set_content(body)
        
        # Send email in thread pool
        await asyncio.get_event_loop().run_in_executor(
            None,
            self._send_email_sync,
            msg
        )
    
    def _send_email_sync(self, msg: EmailMessage) -> None:
        """Synchronously send email."""
        try:
            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                if self.use_tls:
                    server.starttls()
                server.login(self.username, self.password)
                server.send_message(msg)
                
            logger.info(
                "alert_sent_email",
                to=msg["To"],
                subject=msg["Subject"]
            )
            
        except Exception as e:
            logger.error(
                "alert_email_failed",
                error=str(e)
            )


class SlackDestination(AlertDestination):
    """Send alerts to Slack."""
    
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url
    
    async def send(self, alert: Alert) -> None:
        """Send alert to Slack."""
        # Format message for Slack
        color = {
            AlertSeverity.INFO: "#36a64f",
            AlertSeverity.WARNING: "#ff9800",
            AlertSeverity.ERROR: "#f44336",
            AlertSeverity.CRITICAL: "#b71c1c"
        }.get(alert.severity, "#808080")
        
        payload = {
            "attachments": [{
                "color": color,
                "title": f"{alert.severity.value.upper()}: {alert.rule_name}",
                "text": alert.message,
                "fields": [
                    {
                        "title": key,
                        "value": value,
                        "short": True
                    }
                    for key, value in alert.labels.items()
                ],
                "footer": "Workflow Orchestrator",
                "ts": int(alert.fired_at.timestamp()) if alert.fired_at else None
            }]
        }
        
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    self.webhook_url,
                    json=payload,
                    timeout=30.0
                )
                response.raise_for_status()
                
                logger.info(
                    "alert_sent_slack",
                    alert_id=alert.alert_id
                )
                
            except Exception as e:
                logger.error(
                    "alert_slack_failed",
                    alert_id=alert.alert_id,
                    error=str(e)
                )


@dataclass
class AlertRule:
    """Alert rule definition."""
    name: str
    condition: AlertCondition
    severity: AlertSeverity
    message: str
    labels: Dict[str, str] = field(default_factory=dict)
    annotations: Dict[str, str] = field(default_factory=dict)
    destinations: List[AlertDestination] = field(default_factory=list)
    cooldown: timedelta = field(default_factory=lambda: timedelta(minutes=5))
    enabled: bool = True
    
    # Internal state
    _last_fired: Optional[datetime] = field(default=None, init=False)
    _active_alert: Optional[Alert] = field(default=None, init=False)


class AlertManager:
    """Manages alert rules and delivery."""
    
    def __init__(self):
        self.rules: Dict[str, AlertRule] = {}
        self.alerts: Dict[str, Alert] = {}
        self._metrics_buffer: Dict[str, List[Tuple[datetime, float]]] = defaultdict(list)
        self._running = False
        self._tasks: Set[asyncio.Task] = set()
        self._evaluation_interval = 30.0  # seconds
    
    async def start(self) -> None:
        """Start alert manager."""
        self._running = True
        
        # Start evaluation loop
        eval_task = asyncio.create_task(self._evaluation_loop())
        self._tasks.add(eval_task)
        
        # Start cleanup loop
        cleanup_task = asyncio.create_task(self._cleanup_loop())
        self._tasks.add(cleanup_task)
        
        logger.info("alert_manager_started", rules=len(self.rules))
    
    async def stop(self) -> None:
        """Stop alert manager."""
        self._running = False
        
        for task in self._tasks:
            task.cancel()
        
        await asyncio.gather(*self._tasks, return_exceptions=True)
        
        logger.info("alert_manager_stopped")
    
    def add_rule(self, rule: AlertRule) -> None:
        """Add an alert rule."""
        self.rules[rule.name] = rule
        logger.info("alert_rule_added", rule=rule.name)
    
    def remove_rule(self, rule_name: str) -> None:
        """Remove an alert rule."""
        if rule_name in self.rules:
            del self.rules[rule_name]
            logger.info("alert_rule_removed", rule=rule_name)
    
    async def record_metric(
        self,
        metric_name: str,
        value: float,
        timestamp: Optional[datetime] = None
    ) -> None:
        """Record a metric value for alerting."""
        ts = timestamp or datetime.utcnow()
        self._metrics_buffer[metric_name].append((ts, value))
    
    async def _evaluation_loop(self) -> None:
        """Main alert evaluation loop."""
        while self._running:
            try:
                await asyncio.sleep(self._evaluation_interval)
                await self._evaluate_rules()
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("alert_evaluation_error", error=str(e))
    
    async def _evaluate_rules(self) -> None:
        """Evaluate all alert rules."""
        current_time = datetime.utcnow()
        
        for rule in self.rules.values():
            if not rule.enabled:
                continue
            
            try:
                # Get metric values within window
                metric_values = self._get_metric_values(
                    rule.condition.metric,
                    current_time - rule.condition.window,
                    current_time
                )
                
                # Evaluate condition
                is_firing = rule.condition.evaluate([v for _, v in metric_values])
                
                # Handle state transitions
                if is_firing and not rule._active_alert:
                    # Check cooldown
                    if rule._last_fired and current_time - rule._last_fired < rule.cooldown:
                        continue
                    
                    # Create new alert
                    alert = Alert(
                        alert_id=f"{rule.name}_{int(current_time.timestamp())}",
                        rule_name=rule.name,
                        severity=rule.severity,
                        state=AlertState.FIRING,
                        message=rule.message,
                        labels=rule.labels,
                        annotations=rule.annotations,
                        fired_at=current_time,
                        value=metric_values[-1][1] if metric_values else None
                    )
                    
                    rule._active_alert = alert
                    rule._last_fired = current_time
                    self.alerts[alert.alert_id] = alert
                    
                    # Send alert
                    await self._send_alert(alert, rule.destinations)
                    
                elif not is_firing and rule._active_alert:
                    # Resolve alert
                    alert = rule._active_alert
                    alert.state = AlertState.RESOLVED
                    alert.resolved_at = current_time
                    
                    rule._active_alert = None
                    
                    # Send resolution
                    await self._send_alert(alert, rule.destinations)
                    
            except Exception as e:
                logger.error(
                    "rule_evaluation_error",
                    rule=rule.name,
                    error=str(e)
                )
    
    def _get_metric_values(
        self,
        metric_name: str,
        start_time: datetime,
        end_time: datetime
    ) -> List[Tuple[datetime, float]]:
        """Get metric values within time range."""
        if metric_name not in self._metrics_buffer:
            return []
        
        return [
            (ts, value)
            for ts, value in self._metrics_buffer[metric_name]
            if start_time <= ts <= end_time
        ]
    
    async def _send_alert(
        self,
        alert: Alert,
        destinations: List[AlertDestination]
    ) -> None:
        """Send alert to all destinations."""
        tasks = []
        
        for destination in destinations:
            task = asyncio.create_task(destination.send(alert))
            tasks.append(task)
        
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        
        logger.info(
            "alert_sent",
            alert_id=alert.alert_id,
            state=alert.state.value,
            destinations=len(destinations)
        )
    
    async def _cleanup_loop(self) -> None:
        """Clean up old metrics data."""
        while self._running:
            try:
                await asyncio.sleep(3600)  # Run hourly
                
                cutoff_time = datetime.utcnow() - timedelta(hours=24)
                
                # Clean up old metrics
                for metric_name in list(self._metrics_buffer.keys()):
                    self._metrics_buffer[metric_name] = [
                        (ts, value)
                        for ts, value in self._metrics_buffer[metric_name]
                        if ts > cutoff_time
                    ]
                    
                    if not self._metrics_buffer[metric_name]:
                        del self._metrics_buffer[metric_name]
                
                # Clean up old alerts
                old_alerts = [
                    alert_id
                    for alert_id, alert in self.alerts.items()
                    if alert.resolved_at and alert.resolved_at < cutoff_time
                ]
                
                for alert_id in old_alerts:
                    del self.alerts[alert_id]
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("alert_cleanup_error", error=str(e))


# Alert DSL
class AlertDSL:
    """DSL for defining alerts."""
    
    def __init__(self, name: str):
        self.name = name
        self._metric: Optional[str] = None
        self._operator: Optional[str] = None
        self._threshold: Optional[float] = None
        self._window: Optional[timedelta] = None
        self._aggregation: str = "avg"
        self._severity: AlertSeverity = AlertSeverity.WARNING
        self._message: str = ""
        self._labels: Dict[str, str] = {}
        self._annotations: Dict[str, str] = {}
        self._destinations: List[AlertDestination] = []
        self._cooldown: timedelta = timedelta(minutes=5)
    
    def where(self, metric: str) -> "AlertDSL":
        """Set the metric to monitor."""
        self._metric = metric
        return self
    
    def is_above(self, threshold: float) -> "AlertDSL":
        """Alert when metric is above threshold."""
        self._operator = ">"
        self._threshold = threshold
        return self
    
    def is_below(self, threshold: float) -> "AlertDSL":
        """Alert when metric is below threshold."""
        self._operator = "<"
        self._threshold = threshold
        return self
    
    def equals(self, threshold: float) -> "AlertDSL":
        """Alert when metric equals threshold."""
        self._operator = "=="
        self._threshold = threshold
        return self
    
    def in_window(self, minutes: int) -> "AlertDSL":
        """Set the time window for evaluation."""
        self._window = timedelta(minutes=minutes)
        return self
    
    def aggregate_by(self, method: str) -> "AlertDSL":
        """Set aggregation method (avg, sum, min, max, count)."""
        self._aggregation = method
        return self
    
    def with_severity(self, severity: AlertSeverity) -> "AlertDSL":
        """Set alert severity."""
        self._severity = severity
        return self
    
    def with_message(self, message: str) -> "AlertDSL":
        """Set alert message."""
        self._message = message
        return self
    
    def with_labels(self, **labels) -> "AlertDSL":
        """Add labels to alert."""
        self._labels.update(labels)
        return self
    
    def with_annotations(self, **annotations) -> "AlertDSL":
        """Add annotations to alert."""
        self._annotations.update(annotations)
        return self
    
    def then(self, *destinations: AlertDestination) -> "AlertDSL":
        """Add alert destinations."""
        self._destinations.extend(destinations)
        return self
    
    def with_cooldown(self, minutes: int) -> "AlertDSL":
        """Set cooldown period between alerts."""
        self._cooldown = timedelta(minutes=minutes)
        return self
    
    def build(self) -> AlertRule:
        """Build the alert rule."""
        if not all([self._metric, self._operator, self._threshold, self._window]):
            raise ValueError("Incomplete alert rule definition")
        
        condition = AlertCondition(
            metric=self._metric,
            operator=self._operator,
            threshold=self._threshold,
            window=self._window,
            aggregation=self._aggregation
        )
        
        return AlertRule(
            name=self.name,
            condition=condition,
            severity=self._severity,
            message=self._message or f"Alert: {self.name}",
            labels=self._labels,
            annotations=self._annotations,
            destinations=self._destinations,
            cooldown=self._cooldown
        )


def alert_dsl(name: str) -> AlertDSL:
    """Create an alert using the DSL."""
    return AlertDSL(name)


# Example usage of the alert DSL:
"""
# Create alert manager
manager = AlertManager()

# Define alerts using DSL
high_error_rate = (
    alert_dsl("high_error_rate")
    .where("workflow.errors.rate")
    .is_above(0.1)
    .in_window(5)
    .with_severity(AlertSeverity.ERROR)
    .with_message("Error rate exceeded 10% in the last 5 minutes")
    .with_labels(team="platform", component="workflow")
    .then(
        WebhookDestination("https://alerts.example.com/webhook"),
        SlackDestination("https://hooks.slack.com/...")
    )
    .build()
)

manager.add_rule(high_error_rate)
"""