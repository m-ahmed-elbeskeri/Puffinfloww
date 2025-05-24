#!/usr/bin/env python3
"""
examples/dag_example.py

End-to-end demo script for the DAG engine:
  1. Patches rogue Jinja2 loader if needed
  2. Bootstraps minimal Jinja2 templates
  3. Writes a simple YAML workflow
  4. Parses → DAGBuilder → DAG
  5. Generates workflow + test code via CodeGenerator
  6. Executes the DAG with DAGExecutor
  7. Renders & prints a Graphviz visualisation
"""

import sys
import re
from pathlib import Path
import asyncio
from textwrap import dedent
from datetime import datetime

# ─────────────────────────────────────────────────────────────────────────────
# 0. Patch Jinja2 loaders file to remove circular import (if present)
# ─────────────────────────────────────────────────────────────────────────────
try:
    import jinja2.loaders
    loaders_path = Path(jinja2.loaders.__file__)
    text = loaders_path.read_text(encoding='utf-8')
    patched = re.sub(r'^from\s+core\.dag\.builder\s+import.*$', '', text, flags=re.MULTILINE)
    if text != patched:
        loaders_path.write_text(patched, encoding='utf-8')
        print(f"Patched Jinja2 loaders at: {loaders_path}")
except Exception:
    pass

# ─────────────────────────────────────────────────────────────────────────────
# 1. Bootstrap templates (ensure UTF-8 encoding)
# ─────────────────────────────────────────────────────────────────────────────
TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "core" / "dag" / "templates"
TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)

# Minimal workflow template
(TEMPLATE_DIR / "workflow.py.j2").write_text(dedent("""
# Auto-generated workflow: {{ workflow.name }}
# Version: {{ workflow.version }}   - generated {{ generated_at }}

import asyncio
from core.agent import Agent, Context

class {{ workflow.name|title|replace('_','') }}Workflow:
    def __init__(self):
        self.agent = Agent("{{ workflow.name }}")
        self._setup_states()

    def _setup_states(self):
        {% for state_name in workflow.states.keys() %}
        self.agent.add_state(name="{{ state_name }}", func=self.{{ state_name }})
        {% endfor %}

    {% for state_name, state in workflow.states.items() %}
    async def {{ state_name }}(self, context: Context):
        {% if state.config.message %}print({{ state.config.message!r }}){% endif %}
        {% if state.transitions %}return "{{ state.transitions[0].target }}"{% endif %}
    {% endfor %}

async def main():
    await {{ workflow.name|title|replace('_','') }}Workflow().agent.run()

if __name__ == "__main__":
    asyncio.run(main())
"""), encoding='utf-8')

# Minimal state-function template
(TEMPLATE_DIR / "state_function.py.j2").write_text(
    "async def {{ state.name }}(context: Context):\n    pass\n",
    encoding='utf-8'
)

# Minimal pytest template
(TEMPLATE_DIR / "workflow_test.py.j2").write_text(dedent("""
import pytest
import asyncio
from {{ workflow.name }}_auto import {{ workflow.name|title|replace('_','') }}Workflow

@pytest.mark.asyncio
async def test_successful_execution():
    await {{ workflow.name|title|replace('_','') }}Workflow().run()
"""), encoding='utf-8')

# ─────────────────────────────────────────────────────────────────────────────
# 2. Write the YAML definition
# ─────────────────────────────────────────────────────────────────────────────
yaml_text = """
name: demo_workflow
version: "1.0"
start_state: say_hello
end_states: [done]

states:
  say_hello:
    type: task
    config:
      plugin: echo
      message: "Hello, world!"
    transitions: [done]

  done:
    type: task
    config: {}
"""

yaml_file = Path(__file__).resolve().parent / "demo_workflow.yaml"
yaml_file.write_text(yaml_text.strip() + "\n", encoding='utf-8')

# ─────────────────────────────────────────────────────────────────────────────
# 3. Parse & build the DAG
# ─────────────────────────────────────────────────────────────────────────────
from core.dag.parser import YAMLParser
from core.dag.builder import DAGBuilder, CodeGenerator
from core.dag.executer import DAGExecutor
from core.dag.graph import DAGVisualizer

workflow = YAMLParser.parse_file(yaml_file)
builder = DAGBuilder()
dag = builder.build_from_definition(workflow)

# ─────────────────────────────────────────────────────────────────────────────
# 4. Code-generation
# ─────────────────────────────────────────────────────────────────────────────
cg = CodeGenerator()
generated_py   = Path(__file__).resolve().parent / "demo_workflow_auto.py"
generated_test = Path(__file__).resolve().parent / "test_demo_workflow.py"

cg.generate_workflow_code(workflow, output_file=generated_py)
cg.generate_test_code(workflow,    output_file=generated_test)

print(f"[{datetime.utcnow().isoformat()}]  ✔ Code-gen complete")
print(f" • Workflow → {generated_py}")
print(f" • Tests    → {generated_test}\n")

# ─────────────────────────────────────────────────────────────────────────────
# 5. Execute the DAG
# ─────────────────────────────────────────────────────────────────────────────
async def run_exec():
    ctx = await DAGExecutor(max_concurrent=2).execute(dag)
    print("--- Execution Summary ---")
    for name, exec_info in ctx.node_executions.items():
        print(f"{name:<10} : {exec_info.status.value}")

asyncio.run(run_exec())

# ─────────────────────────────────────────────────────────────────────────────
# 6. Visualise with Graphviz
# ─────────────────────────────────────────────────────────────────────────────
dot_src = DAGVisualizer.to_dot(dag, output_file="demo_workflow_graph")
print("\n--- Graphviz DOT ---")
print(dot_src)
print(f"\nPNG written to: {Path('demo_workflow_graph.png').resolve()}")