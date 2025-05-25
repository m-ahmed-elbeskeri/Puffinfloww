import pytest
import asyncio
from demo_workflow_auto import DemoworkflowWorkflow


@pytest.mark.asyncio
async def test_successful_execution():
    workflow = DemoworkflowWorkflow()
    await workflow.run()
