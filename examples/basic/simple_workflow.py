import sys
import os
import asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../")))

from core.agent.base import Agent
from core.agent.context import Context
from core.monitoring.metrics import agent_monitor


async def fetch_data(context: Context):
    """Fetch data from source."""
    print("Fetching data...")
    await asyncio.sleep(1)

    # Use shared variable storage
    context.set_variable("data", {"users": 100, "orders": 250})
    return "process_data"


async def process_data(context: Context):
    """Process the fetched data."""
    data = context.get_variable("data")  # shared_state-backed

    print(f"Processing data: {data}")
    await asyncio.sleep(0.5)

    context.set_variable("results", {
        "total_revenue": data["orders"] * 50,
        "avg_order_value": 50
    })

    return ["generate_report", "send_notification"]


async def generate_report(context: Context):
    """Generate report from results."""
    results = context.get_variable("results")
    print(f"Generating report: Revenue = ${results['total_revenue']}")
    await asyncio.sleep(0.3)

    context.set_variable("report", f"Revenue Report: ${results['total_revenue']}")



async def send_notification(context: Context):
    """Send notification about completion."""
    print("Sending notification...")
    await asyncio.sleep(0.2)


@agent_monitor()
async def main():
    agent = Agent("data_pipeline", max_concurrent=5)

    agent.add_state("fetch_data", fetch_data)
    agent.add_state("process_data", process_data, dependencies={"fetch_data": "required"})
    agent.add_state("generate_report", generate_report, dependencies={"process_data": "required"})
    agent.add_state("send_notification", send_notification, dependencies={"process_data": "required"})

    await agent.run()

    print("\nWorkflow completed!")
    final_context = Context(agent.shared_state)
    print(f"Report: {final_context.get_variable('report')}")



if __name__ == "__main__":
    asyncio.run(main())
