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
from datetime import datetime, timezone

# ─────────────────────────────────────────────────────────────────────────────
# 0. Comprehensive Jinja2 cleanup and environment setup
# ─────────────────────────────────────────────────────────────────────────────
def clean_jinja2_environment():
    """Clean any corrupted jinja2.loaders file and prepare clean environment."""
    try:
        # Clear any cached jinja2 modules first
        modules_to_clear = [key for key in sys.modules.keys() if key.startswith('jinja2')]
        for module in modules_to_clear:
            del sys.modules[module]
        
        # Now try to find and clean the jinja2 installation
        import jinja2
        jinja2_path = Path(jinja2.__file__).parent
        loaders_path = jinja2_path / "loaders.py"
        
        if loaders_path.exists():
            text = loaders_path.read_text(encoding='utf-8')
            original_text = text
            
            # Remove any problematic imports from our project
            patterns_to_remove = [
                r'^from\s+core\.dag\.builder\s+import.*$',
                r'^cg\s*=\s*CodeGenerator\(\).*$',
                r'^print\(\"\[DEBUG\].*$',
                r'.*DAGBuilder.*CodeGenerator.*'
            ]
            
            for pattern in patterns_to_remove:
                text = re.sub(pattern, '', text, flags=re.MULTILINE)
            
            # Clean up extra whitespace
            text = re.sub(r'\n\s*\n\s*\n', '\n\n', text)
            text = re.sub(r'\n\s*$', '\n', text)
            
            if text != original_text:
                # Backup the original first
                backup_path = loaders_path.with_suffix('.py.backup')
                backup_path.write_text(original_text, encoding='utf-8')
                
                # Write cleaned version
                loaders_path.write_text(text, encoding='utf-8')
                print(f"✔ Cleaned and backed up Jinja2 loaders: {loaders_path}")
                print(f"  Backup saved to: {backup_path}")
                
                # Clear modules again after fixing
                modules_to_clear = [key for key in sys.modules.keys() if key.startswith('jinja2')]
                for module in modules_to_clear:
                    del sys.modules[module]
                    
                return True
        
        return False
        
    except Exception as e:
        print(f"Warning: Could not clean jinja2 environment: {e}")
        print("Recommendation: Run 'pip uninstall jinja2 && pip install jinja2'")
        return False

def setup_safe_jinja2():
    """Setup a safe jinja2 environment for our use."""
    try:
        # Try to import jinja2 safely
        import jinja2
        print(f"✔ Jinja2 {jinja2.__version__} loaded successfully from: {jinja2.__file__}")
        return True
    except Exception as e:
        print(f"✗ Failed to load jinja2: {e}")
        return False

# Run the cleanup and setup
print("🔧 Setting up clean Jinja2 environment...")
cleaned = clean_jinja2_environment()
if cleaned:
    print("✔ Jinja2 environment cleaned")

if not setup_safe_jinja2():
    print("❌ Cannot proceed without working Jinja2. Please reinstall jinja2.")
    sys.exit(1)

# ─────────────────────────────────────────────────────────────────────────────
# 1. Bootstrap templates (ensure UTF-8 encoding)
# ─────────────────────────────────────────────────────────────────────────────
TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "core" / "dag" / "templates"
TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)

print(f"📁 Setting up templates in: {TEMPLATE_DIR}")

# Fixed workflow template with proper indentation and newlines
workflow_template = dedent("""
# Auto-generated workflow: {{ workflow.name }}
# Version: {{ workflow.version }} - Generated {{ generated_at }}

import asyncio
from core.agent import Agent, Context

class {{ workflow.name|title|replace('_','') }}Workflow:
    def __init__(self):
        self.agent = Agent("{{ workflow.name }}")
        self._setup_states()

    def _setup_states(self):
{%- for state_name in workflow.states.keys() %}
        self.agent.add_state(name="{{ state_name }}", func=self.{{ state_name }})
{%- endfor %}

{%- for state_name, state in workflow.states.items() %}
    async def {{ state_name }}(self, context: Context):
{%- if state.config.get('message') %}
        print({{ state.config.message|tojson }})
{%- endif %}
{%- if state.transitions %}
        return "{{ state.transitions[0].target }}"
{%- else %}
        pass
{%- endif %}

{%- endfor %}
    async def run(self):
        await self.agent.run()

async def main():
    workflow = {{ workflow.name|title|replace('_','') }}Workflow()
    await workflow.run()

if __name__ == "__main__":
    asyncio.run(main())
""").strip()

(TEMPLATE_DIR / "workflow.py.j2").write_text(workflow_template, encoding='utf-8')

# Minimal state-function template
state_template = "async def {{ state.name }}(context: Context):\n    pass\n"
(TEMPLATE_DIR / "state_function.py.j2").write_text(state_template, encoding='utf-8')

# Minimal pytest template
test_template = dedent("""
import pytest
import asyncio
from {{ workflow.name }}_auto import {{ workflow.name|title|replace('_','') }}Workflow

@pytest.mark.asyncio
async def test_successful_execution():
    workflow = {{ workflow.name|title|replace('_','') }}Workflow()
    await workflow.run()
""").strip()

(TEMPLATE_DIR / "workflow_test.py.j2").write_text(test_template, encoding='utf-8')

print("✔ Templates created successfully")

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
print(f"✔ YAML workflow written to: {yaml_file}")

# ─────────────────────────────────────────────────────────────────────────────
# 3. Parse & build the DAG
# ─────────────────────────────────────────────────────────────────────────────
print("\n🏗️  Building DAG from YAML...")

try:
    from core.dag.parser import YAMLParser
    from core.dag.builder import DAGBuilder, CodeGenerator
    from core.dag.executor import DAGExecutor
    from core.dag.graph import DAGVisualizer
    
    print("✔ All DAG modules imported successfully")
    
except ImportError as e:
    print(f"❌ Failed to import DAG modules: {e}")
    print("Make sure all required modules are available.")
    sys.exit(1)

try:
    workflow = YAMLParser.parse_file(yaml_file)
    builder = DAGBuilder()
    dag = builder.build_from_definition(workflow)
    print("✔ DAG built successfully")
    
except Exception as e:
    print(f"❌ Failed to build DAG: {e}")
    sys.exit(1)

# ─────────────────────────────────────────────────────────────────────────────
# 4. Code-generation
# ─────────────────────────────────────────────────────────────────────────────
print("\n📝 Generating code...")

try:
    cg = CodeGenerator()
    generated_py = Path(__file__).resolve().parent / "demo_workflow_auto.py"
    generated_test = Path(__file__).resolve().parent / "test_demo_workflow.py"

    # Generate workflow code
    workflow_code = cg.generate_workflow_code(workflow, output_file=generated_py)
    
    # Generate test code
    test_code = cg.generate_test_code(workflow, output_file=generated_test)

    print(f"✔ Code generation complete")
    print(f"  • Workflow → {generated_py}")
    print(f"  • Tests    → {generated_test}")
    
except Exception as e:
    print(f"❌ Code generation failed: {e}")
    print("Continuing without code generation...")

# ─────────────────────────────────────────────────────────────────────────────
# 5. Execute the DAG
# ─────────────────────────────────────────────────────────────────────────────
print("\n⚡ Executing DAG...")

async def run_execution():
    try:
        executor = DAGExecutor(max_concurrent=2)
        ctx = await executor.execute(dag)
        
        print("✔ DAG execution completed")
        print("\n--- Execution Summary ---")
        for name, exec_info in ctx.node_executions.items():
            status = exec_info.status.value if hasattr(exec_info.status, 'value') else str(exec_info.status)
            print(f"  {name:<12} : {status}")
            
    except Exception as e:
        print(f"❌ DAG execution failed: {e}")

try:
    asyncio.run(run_execution())
except Exception as e:
    print(f"❌ Execution error: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# 6. Visualise with Graphviz
# ─────────────────────────────────────────────────────────────────────────────
print("\n📊 Generating visualization...")

try:
    dot_src = DAGVisualizer.to_dot(dag, output_file="demo_workflow_graph")
    print("✔ Graphviz visualization generated")
    print("\n--- Graphviz DOT ---")
    print(dot_src)
    
    png_path = Path("demo_workflow_graph.png").resolve()
    if png_path.exists():
        print(f"\n✔ PNG written to: {png_path}")
    else:
        print(f"\n⚠️  PNG not found at: {png_path}")
        print("   (Graphviz might not be installed)")
        print("   Install from: https://graphviz.org/download/")
        
except Exception as e:
    print(f"❌ Visualization failed: {e}")

print(f"\n🎉 Demo completed at {datetime.now(timezone.utc).isoformat()}")