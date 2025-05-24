"""Worker CLI implementation."""

import click
import asyncio
import signal
import sys
from typing import Optional
import structlog

from worker.runner import WorkerRunner
from core.config import get_settings


logger = structlog.get_logger(__name__)


def setup_signal_handlers(runner: WorkerRunner):
    """Setup signal handlers for graceful shutdown."""
    def signal_handler(sig, frame):
        logger.info("shutdown_signal_received", signal=sig)
        asyncio.create_task(runner.stop())
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)


@click.command()
@click.option(
    '--queue',
    '-q',
    default='default',
    help='Queue name to process'
)
@click.option(
    '--concurrency',
    '-c',
    type=int,
    help='Number of concurrent tasks'
)
@click.option(
    '--timeout',
    '-t',
    type=float,
    help='Task timeout in seconds'
)
@click.option(
    '--id',
    'worker_id',
    help='Worker ID (auto-generated if not provided)'
)
def main(
    queue: str,
    concurrency: Optional[int],
    timeout: Optional[float],
    worker_id: Optional[str]
):
    """Workflow orchestrator worker."""
    settings = get_settings()
    
    # Create worker runner
    runner = WorkerRunner(
        queue_name=queue,
        worker_id=worker_id,
        concurrency=concurrency or settings.worker_concurrency,
        timeout=timeout or settings.worker_timeout
    )
    
    # Setup signal handlers
    setup_signal_handlers(runner)
    
    # Run worker
    logger.info(
        "worker_starting",
        queue=queue,
        concurrency=runner.concurrency,
        worker_id=runner.worker_id
    )
    
    try:
        asyncio.run(runner.run())
    except KeyboardInterrupt:
        logger.info("worker_interrupted")
    except Exception as e:
        logger.error("worker_error", error=str(e))
        sys.exit(1)
    
    logger.info("worker_stopped")


if __name__ == "__main__":
    main()