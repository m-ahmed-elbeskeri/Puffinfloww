"""DAG builder and code generation from YAML."""

from typing import Dict, List, Optional, Any, Type, Union, Tuple
from pathlib import Path
import textwrap
import black
import jinja2
from datetime import datetime
import structlog

from core.dag.graph import DAG, DAGNode, DAGEdge
from core.dag.parser import YAMLParser, WorkflowDefinition, StateDefinition
from core.agent.base import Agent
from plugins.base import Plugin, PluginState


logger = structlog.get_logger(__name__)


class DAGBuilder:
    """Build DAG from workflow definition."""
    
    def __init__(self):
        self._plugins: Dict[str, Plugin] = {}
    
    def register_plugin(self, plugin: Plugin) -> None:
        """Register a plugin."""
        self._plugins[plugin.manifest.name] = plugin
    
    def build_from_definition(self, workflow: WorkflowDefinition) -> DAG[StateDefinition]:
        """Build DAG from workflow definition."""
        dag = DAG(workflow.name)
        
        # Add nodes
        for state_name, state in workflow.states.items():
            node = DAGNode(
                id=state_name,
                data=state,
                metadata={
                    "label": state.metadata.get("label", state_name),
                    "type": state.type,
                    "plugin": state.config.get("plugin")
                }
            )
            dag.add_node(node)
        
        # Add edges from transitions
        for state_name, state in workflow.states.items():
            for transition in state.transitions:
                edge = DAGEdge(
                    source=state_name,
                    target=transition.target,
                    condition=transition.condition,
                    metadata=transition.metadata
                )
                dag.add_edge(edge)
        
        # Add edges from dependencies
        for state_name, state in workflow.states.items():
            for dep in state.dependencies:
                # Check if edge already exists
                existing_edges = dag.get_edges(source=dep, target=state_name)
                if not existing_edges:
                    edge = DAGEdge(
                        source=dep,
                        target=state_name,
                        metadata={"type": "dependency"}
                    )
                    dag.add_edge(edge)
        
        # Validate DAG
        dag.validate()
        
        return dag
    
    def build_agent(self, workflow: WorkflowDefinition, dag: DAG[StateDefinition]) -> Agent:
        """Build agent from workflow and DAG."""
        agent = Agent(name=workflow.name)
        
        # Get topological order
        topo_order = dag.topological_sort()
        
        # Add states in order
        for state_name in topo_order:
            state_def = workflow.states[state_name]
            
            # Create state function
            state_func = self._create_state_function(state_def)
            
            # Determine dependencies
            dependencies = {}
            for dep in state_def.dependencies:
                dependencies[dep] = "required"
            
            # Add state to agent
            agent.add_state(
                name=state_name,
                func=state_func,
                dependencies=dependencies,
                resources=self._convert_resources(state_def.resources),
                max_retries=state_def.retries
            )
        
        return agent
    
    def _create_state_function(self, state_def: StateDefinition):
        """Create state function from definition."""
        plugin_name = state_def.config.get("plugin")
        
        if plugin_name and plugin_name in self._plugins:
            # Use plugin
            plugin = self._plugins[plugin_name]
            return plugin.get_state_function(state_def.type, state_def.config)
        else:
            # Create generic function
            async def state_function(context):
                # Generic implementation
                context.set_state("state_name", state_def.name)
                context.set_state("state_type", state_def.type)
                context.set_state("config", state_def.config)
                
                # Return transitions
                if state_def.transitions:
                    if len(state_def.transitions) == 1:
                        return state_def.transitions[0].target
                    else:
                        # Multiple transitions - would need condition evaluation
                        return [t.target for t in state_def.transitions]
                
                return None
            
            return state_function
    
    def _convert_resources(self, resources):
        """Convert resource definition to agent format."""
        if not resources:
            return None
        
        from core.resources.requirements import ResourceRequirements
        
        return ResourceRequirements(
            cpu_units=resources.cpu_units,
            memory_mb=resources.memory_mb,
            io_weight=resources.io_weight,
            network_weight=resources.network_weight,
            gpu_units=resources.gpu_units,
            timeout=resources.timeout
        )


class YAMLDAGBuilder(DAGBuilder):
    """Build DAG directly from YAML."""
    
    def build_from_yaml(self, yaml_path: Union[str, Path]) -> Tuple[WorkflowDefinition, DAG[StateDefinition]]:
        """Build workflow and DAG from YAML file."""
        workflow = YAMLParser.parse_file(yaml_path)
        dag = self.build_from_definition(workflow)
        return workflow, dag
    
    def build_from_yaml_string(self, yaml_string: str) -> Tuple[WorkflowDefinition, DAG[StateDefinition]]:
        """Build workflow and DAG from YAML string."""
        workflow = YAMLParser.parse_string(yaml_string)
        dag = self.build_from_definition(workflow)
        return workflow, dag


class CodeGenerator:
    """Generate Python code from workflow definition."""
    
    def __init__(self, template_dir: Optional[Path] = None):
        self.template_dir = template_dir or Path(__file__).parent / "templates"
        self.jinja_env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(self.template_dir)),
            trim_blocks=True,
            lstrip_blocks=True
        )
    
    def generate_workflow_code(
        self,
        workflow: WorkflowDefinition,
        output_file: Optional[Path] = None
    ) -> str:
        """Generate Python code for workflow."""
        template = self.jinja_env.get_template("workflow.py.j2")
        
        code = template.render(
            workflow=workflow,
            generated_at=datetime.utcnow().isoformat(),
            imports=self._get_required_imports(workflow)
        )
        
        # Format with black
        try:
            code = black.format_str(code, mode=black.Mode())
        except Exception as e:
            logger.warning("code_formatting_failed", error=str(e))
        
        if output_file:
            output_file.write_text(code)
        
        return code
    
    def generate_state_code(self, state: StateDefinition) -> str:
        """Generate code for a single state."""
        template = self.jinja_env.get_template("state_function.py.j2")
        
        return template.render(state=state)
    
    def generate_test_code(
        self,
        workflow: WorkflowDefinition,
        output_file: Optional[Path] = None
    ) -> str:
        """Generate test code for workflow."""
        template = self.jinja_env.get_template("workflow_test.py.j2")
        
        code = template.render(
            workflow=workflow,
            test_cases=self._generate_test_cases(workflow)
        )
        
        # Format with black
        try:
            code = black.format_str(code, mode=black.Mode())
        except Exception as e:
            logger.warning("test_formatting_failed", error=str(e))
        
        if output_file:
            output_file.write_text(code)
        
        return code
    
    def _get_required_imports(self, workflow: WorkflowDefinition) -> List[str]:
        """Determine required imports for workflow."""
        imports = [
            "import asyncio",
            "from core.agent import Agent, Context",
            "from core.resources import ResourceRequirements"
        ]
        
        # Check for specific plugin imports
        plugins = set()
        for state in workflow.states.values():
            if "plugin" in state.config:
                plugins.add(state.config["plugin"])
        
        for plugin in plugins:
            imports.append(f"from plugins.{plugin} import {plugin.title()}Plugin")
        
        return imports
    
    def _generate_test_cases(self, workflow: WorkflowDefinition) -> List[Dict[str, Any]]:
        """Generate test cases for workflow."""
        test_cases = []
        
        # Test case for successful path
        test_cases.append({
            "name": "test_successful_execution",
            "description": "Test successful workflow execution",
            "setup": {},
            "expected_states": list(workflow.states.keys())
        })
        
        # Test case for each branch
        for state_name, state in workflow.states.items():
            if len(state.transitions) > 1:
                for i, transition in enumerate(state.transitions):
                    test_cases.append({
                        "name": f"test_{state_name}_branch_{i}",
                        "description": f"Test {state_name} -> {transition.target}",
                        "setup": {"condition": transition.condition},
                        "expected_transition": transition.target
                    })
        
        return test_cases


class TemplateEngine:
    """Template engine for code generation."""
    
    def __init__(self):
        self.templates: Dict[str, jinja2.Template] = {}
        self._load_builtin_templates()
    
    def _load_builtin_templates(self):
        """Load built-in templates."""
        # Workflow template
        self.templates["workflow"] = jinja2.Template("""
# Generated workflow: {{ workflow.name }}
# Version: {{ workflow.version }}
# Generated at: {{ generated_at }}

import asyncio
from core import Agent, Context
from core.resources import ResourceRequirements

{% for import in imports %}
{{ import }}
{% endfor %}


class {{ workflow.name|title|replace('_', '') }}Workflow:
    \"\"\"{{ workflow.description }}\"\"\"
    
    def __init__(self):
        self.agent = Agent("{{ workflow.name }}")
        self._setup_states()
    
    def _setup_states(self):
        \"\"\"Setup workflow states.\"\"\"
        {% for state_name, state in workflow.states.items() %}
        # State: {{ state_name }}
        self.agent.add_state(
            name="{{ state_name }}",
            func=self.{{ state_name }},
            {% if state.dependencies %}
            dependencies={
                {% for dep in state.dependencies %}
                "{{ dep }}": "required",
                {% endfor %}
            },
            {% endif %}
            {% if state.resources %}
            resources=ResourceRequirements(
                cpu_units={{ state.resources.cpu_units }},
                memory_mb={{ state.resources.memory_mb }},
                {% if state.resources.timeout %}
                timeout={{ state.resources.timeout }}
                {% endif %}
            ),
            {% endif %}
            max_retries={{ state.retries }}
        )
        
        {% endfor %}
    
    {% for state_name, state in workflow.states.items() %}
    async def {{ state_name }}(self, context: Context):
        \"\"\"{{ state.metadata.get('description', state_name) }}\"\"\"
        # State implementation
        {% if state.config %}
        config = {{ state.config }}
        {% endif %}
        
        # TODO: Implement state logic
        
        {% if state.transitions|length == 1 %}
        return "{{ state.transitions[0].target }}"
        {% elif state.transitions|length > 1 %}
        # Multiple transitions - evaluate conditions
        {% for transition in state.transitions %}
        {% if transition.condition %}
        if {{ transition.condition }}:
            return "{{ transition.target }}"
        {% endif %}
        {% endfor %}
        {% endif %}
    
    {% endfor %}
    
    async def run(self):
        \"\"\"Run the workflow.\"\"\"
        await self.agent.run()


async def main():
    \"\"\"Run the workflow.\"\"\"
    workflow = {{ workflow.name|title|replace('_', '') }}Workflow()
    await workflow.run()


if __name__ == "__main__":
    asyncio.run(main())
""")
        
        # State function template
        self.templates["state_function"] = jinja2.Template("""
async def {{ state.name }}(context: Context):
    \"\"\"
    State: {{ state.name }}
    Type: {{ state.type }}
    \"\"\"
    {% if state.config.get('plugin') %}
    # Using plugin: {{ state.config.plugin }}
    plugin = {{ state.config.plugin|title }}Plugin()
    result = await plugin.execute(context, {{ state.config }})
    {% else %}
    # Custom implementation
    # TODO: Implement state logic
    pass
    {% endif %}
    
    {% if state.transitions %}
    # Transitions
    {% for transition in state.transitions %}
    {% if transition.condition %}
    if {{ transition.condition }}:
        return "{{ transition.target }}"
    {% else %}
    return "{{ transition.target }}"
    {% endif %}
    {% endfor %}
    {% endif %}
""")
    
    def render(self, template_name: str, **context) -> str:
        """Render a template."""
        if template_name not in self.templates:
            raise ValueError(f"Template '{template_name}' not found")
        
        return self.templates[template_name].render(**context)
    
    def add_template(self, name: str, template: Union[str, jinja2.Template]) -> None:
        """Add a custom template."""
        if isinstance(template, str):
            template = jinja2.Template(template)
        
        self.templates[name] = template


from typing import Union, Tuple