"""Cron-based scheduling implementation."""

import re
from typing import Optional, List, Tuple, Dict, Any
from datetime import datetime, timedelta
from dataclasses import dataclass
import structlog
from croniter import croniter

from core.scheduler.base import Scheduler, Schedule, ScheduleType


logger = structlog.get_logger(__name__)


@dataclass
class CronExpression:
    """Parsed cron expression."""
    minute: str = "*"
    hour: str = "*"
    day: str = "*"
    month: str = "*"
    weekday: str = "*"
    second: Optional[str] = None  # Extended cron with seconds
    year: Optional[str] = None  # Extended cron with year
    
    def __str__(self):
        """Convert back to cron string."""
        parts = [self.minute, self.hour, self.day, self.month, self.weekday]
        
        if self.second is not None:
            parts.insert(0, self.second)
        if self.year is not None:
            parts.append(self.year)
        
        return " ".join(parts)
    
    def to_dict(self) -> Dict[str, str]:
        """Convert to dictionary."""
        return {
            "minute": self.minute,
            "hour": self.hour,
            "day": self.day,
            "month": self.month,
            "weekday": self.weekday,
            "second": self.second,
            "year": self.year
        }


def parse_cron(expression: str) -> CronExpression:
    """
    Parse a cron expression.
    
    Supports standard 5-field cron and extended 6/7-field formats.
    
    Standard: minute hour day month weekday
    Extended: second minute hour day month weekday [year]
    
    Special strings:
    - @yearly, @annually - Run once a year at midnight on January 1st
    - @monthly - Run once a month at midnight on the first day
    - @weekly - Run once a week at midnight on Sunday
    - @daily, @midnight - Run once a day at midnight
    - @hourly - Run once an hour at the beginning
    """
    expression = expression.strip()
    
    # Handle special strings
    special_expressions = {
        "@yearly": "0 0 1 1 *",
        "@annually": "0 0 1 1 *",
        "@monthly": "0 0 1 * *",
        "@weekly": "0 0 * * 0",
        "@daily": "0 0 * * *",
        "@midnight": "0 0 * * *",
        "@hourly": "0 * * * *"
    }
    
    if expression in special_expressions:
        expression = special_expressions[expression]
    
    # Split expression
    parts = expression.split()
    
    if len(parts) == 5:
        # Standard cron
        return CronExpression(
            minute=parts[0],
            hour=parts[1],
            day=parts[2],
            month=parts[3],
            weekday=parts[4]
        )
    elif len(parts) == 6:
        # Extended with seconds
        return CronExpression(
            second=parts[0],
            minute=parts[1],
            hour=parts[2],
            day=parts[3],
            month=parts[4],
            weekday=parts[5]
        )
    elif len(parts) == 7:
        # Extended with seconds and year
        return CronExpression(
            second=parts[0],
            minute=parts[1],
            hour=parts[2],
            day=parts[3],
            month=parts[4],
            weekday=parts[5],
            year=parts[6]
        )
    else:
        raise ValueError(f"Invalid cron expression: {expression}")


class CronSchedule(Schedule):
    """Schedule with cron expression."""
    
    def __init__(
        self,
        schedule_id: str,
        name: str,
        cron_expression: str,
        timezone: Optional[str] = None,
        **kwargs
    ):
        # Parse cron expression
        self.cron_expr = parse_cron(cron_expression)
        self.cron_string = str(self.cron_expr)
        self.timezone = timezone or "UTC"
        
        # Initialize croniter
        self._croniter = croniter(self.cron_string)
        
        config = {
            "cron_expression": self.cron_string,
            "timezone": self.timezone
        }
        
        super().__init__(
            schedule_id=schedule_id,
            name=name,
            type=ScheduleType.CRON,
            config=config,
            **kwargs
        )
    
    def get_next_run(self, base_time: Optional[datetime] = None) -> datetime:
        """Get next run time based on cron expression."""
        base = base_time or datetime.utcnow()
        
        # Reset croniter base time
        self._croniter.set_current(base)
        
        # Get next time
        next_time = self._croniter.get_next(datetime)
        
        return next_time
    
    def get_schedule_description(self) -> str:
        """Get human-readable description of schedule."""
        # This could be enhanced with a cron description library
        descriptions = {
            "0 0 * * *": "Daily at midnight",
            "0 * * * *": "Every hour",
            "*/5 * * * *": "Every 5 minutes",
            "0 0 * * 0": "Weekly on Sunday at midnight",
            "0 0 1 * *": "Monthly on the 1st at midnight",
            "0 0 1 1 *": "Yearly on January 1st at midnight"
        }
        
        return descriptions.get(self.cron_string, f"Cron: {self.cron_string}")


class CronScheduler(Scheduler):
    """Scheduler for cron-based schedules."""
    
    async def _calculate_next_run(self, schedule: Schedule) -> Optional[datetime]:
        """Calculate next run time for cron schedule."""
        if schedule.type != ScheduleType.CRON:
            return None
        
        cron_expr = schedule.config.get("cron_expression")
        if not cron_expr:
            logger.error(
                "no_cron_expression",
                schedule_id=schedule.schedule_id
            )
            return None
        
        try:
            # Create croniter instance
            base_time = schedule.last_run or datetime.utcnow()
            cron = croniter(cron_expr, base_time)
            
            # Get next run time
            next_run = cron.get_next(datetime)
            
            return next_run
            
        except Exception as e:
            logger.error(
                "cron_calculation_error",
                schedule_id=schedule.schedule_id,
                cron_expr=cron_expr,
                error=str(e)
            )
            return None
    
    def add_cron_schedule(
        self,
        name: str,
        cron_expression: str,
        workflow_id: Optional[str] = None,
        agent_config: Optional[Dict[str, Any]] = None,
        timezone: Optional[str] = None,
        **kwargs
    ) -> CronSchedule:
        """Add a cron-based schedule."""
        schedule = CronSchedule(
            schedule_id=str(uuid.uuid4()),
            name=name,
            cron_expression=cron_expression,
            timezone=timezone,
            workflow_id=workflow_id,
            agent_config=agent_config,
            **kwargs
        )
        
        asyncio.create_task(self.add_schedule(schedule))
        
        return schedule
    
    def validate_cron_expression(self, expression: str) -> Tuple[bool, Optional[str]]:
        """
        Validate a cron expression.
        
        Returns:
            Tuple of (is_valid, error_message)
        """
        try:
            # Try to parse
            parsed = parse_cron(expression)
            
            # Try to create croniter
            croniter(str(parsed))
            
            return True, None
            
        except Exception as e:
            return False, str(e)
    
    def get_next_n_runs(
        self,
        schedule_id: str,
        n: int = 10,
        base_time: Optional[datetime] = None
    ) -> List[datetime]:
        """Get next N run times for a schedule."""
        schedule = self.get_schedule(schedule_id)
        if not schedule or schedule.type != ScheduleType.CRON:
            return []
        
        cron_expr = schedule.config.get("cron_expression")
        if not cron_expr:
            return []
        
        try:
            base = base_time or datetime.utcnow()
            cron = croniter(cron_expr, base)
            
            runs = []
            for _ in range(n):
                next_run = cron.get_next(datetime)
                runs.append(next_run)
            
            return runs
            
        except Exception as e:
            logger.error(
                "cron_preview_error",
                schedule_id=schedule_id,
                error=str(e)
            )
            return []


# Cron field validators
class CronValidator:
    """Validates individual cron fields."""
    
    @staticmethod
    def validate_minute(value: str) -> bool:
        """Validate minute field (0-59)."""
        return CronValidator._validate_field(value, 0, 59)
    
    @staticmethod
    def validate_hour(value: str) -> bool:
        """Validate hour field (0-23)."""
        return CronValidator._validate_field(value, 0, 23)
    
    @staticmethod
    def validate_day(value: str) -> bool:
        """Validate day field (1-31)."""
        return CronValidator._validate_field(value, 1, 31)
    
    @staticmethod
    def validate_month(value: str) -> bool:
        """Validate month field (1-12)."""
        # Also support month names
        month_names = {
            "jan": 1, "feb": 2, "mar": 3, "apr": 4,
            "may": 5, "jun": 6, "jul": 7, "aug": 8,
            "sep": 9, "oct": 10, "nov": 11, "dec": 12
        }
        
        if value.lower() in month_names:
            return True
        
        return CronValidator._validate_field(value, 1, 12)
    
    @staticmethod
    def validate_weekday(value: str) -> bool:
        """Validate weekday field (0-7, where 0 and 7 are Sunday)."""
        # Also support day names
        day_names = {
            "sun": 0, "mon": 1, "tue": 2, "wed": 3,
            "thu": 4, "fri": 5, "sat": 6
        }
        
        if value.lower() in day_names:
            return True
        
        return CronValidator._validate_field(value, 0, 7)
    
    @staticmethod
    def _validate_field(value: str, min_val: int, max_val: int) -> bool:
        """Validate a cron field value."""
        if value == "*":
            return True
        
        # Handle step values (*/n)
        if value.startswith("*/"):
            try:
                step = int(value[2:])
                return 1 <= step <= max_val
            except ValueError:
                return False
        
        # Handle ranges (n-m)
        if "-" in value:
            try:
                parts = value.split("-")
                if len(parts) != 2:
                    return False
                start, end = int(parts[0]), int(parts[1])
                return min_val <= start <= end <= max_val
            except ValueError:
                return False
        
        # Handle lists (n,m,...)
        if "," in value:
            parts = value.split(",")
            for part in parts:
                if not CronValidator._validate_field(part.strip(), min_val, max_val):
                    return False
            return True
        
        # Single value
        try:
            val = int(value)
            return min_val <= val <= max_val
        except ValueError:
            return False


import uuid