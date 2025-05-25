# Auto-generated workflow: demo_workflow
# Version: 1.0 - Generated 2025-05-25T00:41:18.105991

import asyncio
from core.agent import Agent, Context

class DemoworkflowWorkflow:
    def __init__(self):
        self.agent = Agent("demo_workflow")
        self._setup_states()

    def _setup_states(self):        self.agent.add_state(name="say_hello", func=self.say_hello)        self.agent.add_state(name="done", func=self.done)    async def say_hello(self, context: Context):        print("Hello, world!")        return "done"    async def done(self, context: Context):        pass    async def run(self):
        await self.agent.run()

async def main():
    workflow = DemoworkflowWorkflow()
    await workflow.run()

if __name__ == "__main__":
    asyncio.run(main())