"""
full_context_workflow.py – Exhaustively tests the Context API.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import Any, Dict

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../")))

from pydantic import BaseModel

from core.agent.base import Agent
from core.agent.context import Context
from core.monitoring.metrics import agent_monitor


# ────────────────────────────── Pydantic models ──────────────────────────── #

class ResultsModel(BaseModel):
    total_revenue: float
    avg_order_value: float
    processed_at: float


class ConfigModel(BaseModel):
    retries: int
    debug: bool = False


# ────────────────────────────────── States ───────────────────────────────── #

async def init_setup(ctx: Context) -> str:
    """Initialise constants, secrets, a typed-variable, and validated data."""
    ctx.set_constant("API_URL", "https://api.example.com/v1")
    ctx.set_secret("slack_webhook", "super-secret-token-123")

    ctx.set_variable("visits", 0)                       # free-form variable
    ctx.set_typed_variable("counter", 0)                # type-locked variable
    ctx.set_validated_data("config", ConfigModel(retries=3))  # Pydantic-locked

    return "fetch_data"


async def fetch_data(ctx: Context) -> str:
    """Pretend to hit an API and store raw data + timestamp."""
    print("Fetching data from", ctx.get_constant("API_URL"))
    await asyncio.sleep(0.2)

    ctx.set_variable("data", {"users": 120, "orders": 275})
    ctx.set_variable("fetched_at", time.time())

    ctx.set_variable("visits", ctx.get_variable("visits") + 1)

    return "process_data"


async def process_data(ctx: Context) -> list[str]:
    """Transform data, create shared typed artefacts, update counters."""
    data: Dict[str, Any] = ctx.get_variable("data")
    print("Processing:", data)
    await asyncio.sleep(0.2)

    results = ResultsModel(
        total_revenue=data["orders"] * 50,
        avg_order_value=50,
        processed_at=ctx.get_variable("fetched_at"),
    )
    # Shared Pydantic data (visible to later states)
    ctx.set_validated_data("results", results)

    # Demo: per-state output + shared variable
    ctx.set_output("records_processed", data["orders"])
    ctx.set_variable("records_processed", data["orders"])

    # Update type-locked counter (still int → allowed)
    ctx.set_typed_variable("counter", ctx.get_typed_variable("counter", int) + 1)

    # Toggle debug in validated config (same class → allowed)
    cfg = ctx.get_validated_data("config", ConfigModel)
    ctx.set_validated_data("config", cfg.model_copy(update={"debug": True}))

    return ["generate_report", "send_notification"]


async def generate_report(ctx: Context) -> None:
    """Read shared typed results and write a report string."""
    res = ctx.get_validated_data("results", ResultsModel)
    ctx.set_variable("report", f"Revenue Report: ${res.total_revenue:,.0f}")
    await asyncio.sleep(0.05)


async def send_notification(ctx: Context) -> None:
    """Use secret, shared variable, output (None expected), and typed variable."""
    print("Sending Slack notification… (pretend)")
    print("Webhook:", ctx.get_secret("slack_webhook"))
    print("Records (var):", ctx.get_variable("records_processed"))
    print("Records (output, same-state expected None):", ctx.get_output("records_processed"))
    print("Counter value:", ctx.get_typed_variable("counter", int))
    await asyncio.sleep(0.05)


# ───────────────────────────────── Driver ────────────────────────────────── #

@agent_monitor()
async def main() -> None:
    agent = Agent("ctx_demo", max_concurrent=4)

    agent.add_state("init_setup",        init_setup)
    agent.add_state("fetch_data",        fetch_data,        {"init_setup": "required"})
    agent.add_state("process_data",      process_data,      {"fetch_data": "required"})
    agent.add_state("generate_report",   generate_report,   {"process_data": "required"})
    agent.add_state("send_notification", send_notification, {"process_data": "required"})

    await agent.run()

    # ─────────── Post-run inspection & guard-rail demonstrations ────────────
    ctx = Context(agent.shared_state)
    print("\nWorkflow completed!")
    print("Variable keys :", ctx.get_variable_keys())
    print("Variables      :", {k: ctx.get_variable(k) for k in ctx.get_variable_keys()})
    print("Typed-var keys :", list(ctx._typed_var_types))
    print("Validated keys :", list(ctx._validated_types))
    print("Output keys    :", ctx.get_output_keys())
    print("Report         :", ctx.get_variable("report"))

    # Guard 1: constant overwrite
    try:
        ctx.set_variable("const_API_URL", "https://evil.com")
    except KeyError as e:
        print("\n✔ Constant overwrite blocked:", e)

    # Guard 2: type-locked variable overwrite
    try:
        ctx.set_typed_variable("counter", 3.14)      # float vs int
    except TypeError as e:
        print("✔ Type-locked variable blocked :", e)

    # Guard 3: validated model class change
    class AltConfig(BaseModel):
        retries: int
        verbose: bool

    try:
        ctx.set_validated_data("config", AltConfig(retries=5, verbose=True))
    except TypeError as e:
        print("✔ Validated model class blocked :", e)


if __name__ == "__main__":
    asyncio.run(main())
