"""Template system for workflow generation."""

from typing import Dict, List, Optional, Any, Union
from dataclasses import dataclass, field
from pathlib import Path
import yaml
import jinja2
from datetime import datetime
import structlog

from core.dag.parser import WorkflowDefinition, StateDefinition


logger = structlog.get_logger(__name__)


@dataclass
class StateTemplate:
    """Template for a workflow state."""
    name: str
    type: str
    template: str
    variables: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def render(self, **kwargs) -> StateDefinition:
        """Render state from template."""
        # Merge variables
        context = {**self.variables, **kwargs}
        
        # Render template
        template = jinja2.Template(self.template)
        rendered = template.render(**context)
        
        # Parse as YAML
        state_data = yaml.safe_load(rendered)
        
        return StateDefinition.from_dict(self.name, state_data)


@dataclass
class WorkflowTemplate:
    """Template for a complete workflow."""
    name: str
    version: str = "1.0"
    description: str = ""
    template: str = ""
    variables: Dict[str, Any] = field(default_factory=dict)
    states: List[StateTemplate] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def render(self, **kwargs) -> WorkflowDefinition:
        """Render workflow from template."""
        # Merge variables
        context = {**self.variables, **kwargs}
        
        if self.template:
            # Render complete workflow template
            template = jinja2.Template(self.template)
            rendered = template.render(**context)
            workflow_data = yaml.safe_load(rendered)
        else:
            # Build from state templates
            workflow_data = {
                "name": self.name,
                "version": self.version,
                "description": self.description,
                "metadata": self.metadata,
                "states": {}
            }
            
            # Render each state
            for state_template in self.states:
                state = state_template.render(**context)
                workflow_data["states"][state.name] = {
                    "type": state.type,
                    "config": state.config,
                    "dependencies": state.dependencies,
                    "transitions": [
                        {"target": t.target, "condition": t.condition}
                        for t in state.transitions
                    ],
                    "resources": {
                        "cpu": state.resources.cpu_units,
                        "memory": state.resources.memory_mb
                    } if state.resources else None,
                    "retries": state.retries,
                    "metadata": state.metadata
                }
        
        return WorkflowDefinition.from_dict(workflow_data)


class TemplateLibrary:
    """Library of workflow templates."""
    
    def __init__(self, template_dir: Optional[Path] = None):
        self.template_dir = template_dir or Path(__file__).parent / "templates"
        self._templates: Dict[str, WorkflowTemplate] = {}
        self._state_templates: Dict[str, StateTemplate] = {}
        self._load_builtin_templates()
    
    def _load_builtin_templates(self):
        """Load built-in templates."""
        # ETL Pipeline Template
        self._templates["etl_pipeline"] = WorkflowTemplate(
            name="etl_pipeline",
            description="Extract, Transform, Load pipeline",
            template="""
name: {{ name }}
version: "1.0"
description: "ETL Pipeline for {{ source_name }}"
start_state: extract_data
end_states: [load_complete]

states:
  extract_data:
    type: task
    config:
      plugin: "{{ extractor_plugin }}"
      source: "{{ source_url }}"
      format: "{{ source_format }}"
    transitions:
      - validate_data
    resources:
      cpu: {{ extract_cpu | default(1) }}
      memory: {{ extract_memory | default(512) }}

  validate_data:
    type: task
    config:
      plugin: "validation"
      rules: {{ validation_rules | tojson }}
    dependencies: [extract_data]
    transitions:
      - target: transform_data
        condition: "valid == true"
      - target: handle_invalid
        condition: "valid == false"

  transform_data:
    type: task
    config:
      plugin: "{{ transformer_plugin }}"
      operations: {{ transform_operations | tojson }}
    dependencies: [validate_data]
    transitions:
      - load_data
    resources:
      cpu: {{ transform_cpu | default(2) }}
      memory: {{ transform_memory | default(1024) }}

  load_data:
    type: task
    config:
      plugin: "{{ loader_plugin }}"
      destination: "{{ destination_url }}"
      format: "{{ destination_format }}"
    dependencies: [transform_data]
    transitions:
      - load_complete
    resources:
      cpu: {{ load_cpu | default(1) }}
      memory: {{ load_memory | default(512) }}

  handle_invalid:
    type: task
    config:
      plugin: "error_handler"
      action: "quarantine"
    transitions:
      - load_complete

  load_complete:
    type: task
    config:
      plugin: "notification"
      message: "ETL pipeline completed"
""",
            variables={
                "name": "my_etl_pipeline",
                "source_name": "data_source",
                "extractor_plugin": "file_extractor",
                "source_url": "s3://bucket/data",
                "source_format": "csv",
                "validation_rules": ["not_null", "type_check"],
                "transformer_plugin": "data_transformer",
                "transform_operations": ["normalize", "aggregate"],
                "loader_plugin": "database_loader",
                "destination_url": "postgres://localhost/db",
                "destination_format": "table"
            }
        )
        
        # ML Pipeline Template
        self._templates["ml_pipeline"] = WorkflowTemplate(
            name="ml_pipeline",
            description="Machine Learning training pipeline",
            template="""
name: {{ name }}
version: "1.0"
description: "ML Pipeline for {{ model_name }}"
start_state: load_data
end_states: [deploy_model]

states:
  load_data:
    type: task
    config:
      plugin: "data_loader"
      dataset: "{{ dataset_path }}"
      split_ratio: {{ split_ratio | default(0.8) }}
    transitions:
      - preprocess_data
    resources:
      cpu: 2
      memory: 4096

  preprocess_data:
    type: task
    config:
      plugin: "preprocessor"
      steps: {{ preprocessing_steps | tojson }}
    dependencies: [load_data]
    transitions:
      - train_model

  train_model:
    type: task
    config:
      plugin: "ml_trainer"
      algorithm: "{{ algorithm }}"
      hyperparameters: {{ hyperparameters | tojson }}
    dependencies: [preprocess_data]
    transitions:
      - evaluate_model
    resources:
      cpu: {{ train_cpu | default(8) }}
      memory: {{ train_memory | default(16384) }}
      gpu: {{ train_gpu | default(1) }}

  evaluate_model:
    type: task
    config:
      plugin: "ml_evaluator"
      metrics: {{ metrics | default(['accuracy', 'f1_score']) | tojson }}
    dependencies: [train_model]
    transitions:
      - target: deploy_model
        condition: "accuracy > {{ accuracy_threshold | default(0.9) }}"
      - target: retrain_model
        condition: "accuracy <= {{ accuracy_threshold | default(0.9) }}"

  retrain_model:
    type: task
    config:
      plugin: "ml_trainer"
      algorithm: "{{ algorithm }}"
      hyperparameters: {{ retrain_hyperparameters | tojson }}
    transitions:
      - evaluate_model

  deploy_model:
    type: task
    config:
      plugin: "model_deployer"
      endpoint: "{{ deployment_endpoint }}"
      version: "{{ model_version }}"
""",
            variables={
                "name": "my_ml_pipeline",
                "model_name": "classifier",
                "dataset_path": "s3://bucket/dataset.csv",
                "algorithm": "random_forest",
                "hyperparameters": {"n_estimators": 100, "max_depth": 10},
                "metrics": ["accuracy", "precision", "recall", "f1_score"],
                "deployment_endpoint": "https://api.example.com/models"
            }
        )
        
        # Data Processing Template
        self._templates["data_processing"] = WorkflowTemplate(
            name="data_processing",
            description="Generic data processing pipeline",
            states=[
                StateTemplate(
                    name="read_data",
                    type="task",
                    template="""
type: task
config:
  plugin: "{{ reader_plugin | default('file_reader') }}"
  path: "{{ input_path }}"
  format: "{{ input_format | default('json') }}"
transitions:
  - process_data
resources:
  cpu: {{ cpu | default(1) }}
  memory: {{ memory | default(1024) }}
"""
                ),
                StateTemplate(
                    name="process_data",
                    type="parallel",
                    template="""
type: parallel
config:
  tasks:
    {% for processor in processors %}
    - {{ processor }}
    {% endfor %}
dependencies: [read_data]
transitions:
  - aggregate_results
"""
                ),
                StateTemplate(
                    name="aggregate_results",
                    type="task",
                    template="""
type: task
config:
  plugin: "aggregator"
  method: "{{ aggregation_method | default('merge') }}"
dependencies: {{ processors | tojson }}
transitions:
  - write_results
"""
                ),
                StateTemplate(
                    name="write_results",
                    type="task",
                    template="""
type: task
config:
  plugin: "{{ writer_plugin | default('file_writer') }}"
  path: "{{ output_path }}"
  format: "{{ output_format | default('json') }}"
dependencies: [aggregate_results]
"""
                )
            ],
            variables={
                "processors": ["filter_data", "transform_data", "enrich_data"],
                "input_path": "/data/input",
                "output_path": "/data/output"
            }
        )
    
    def get_template(self, name: str) -> Optional[WorkflowTemplate]:
        """Get workflow template by name."""
        return self._templates.get(name)
    
    def list_templates(self) -> List[str]:
        """List available templates."""
        return list(self._templates.keys())
    
    def add_template(self, template: WorkflowTemplate) -> None:
        """Add a template to the library."""
        self._templates[template.name] = template
    
    def render_template(self, name: str, **variables) -> WorkflowDefinition:
        """Render a template with variables."""
        template = self.get_template(name)
        if not template:
            raise ValueError(f"Template '{name}' not found")
        
        return template.render(**variables)
    
    def load_from_file(self, file_path: Path) -> WorkflowTemplate:
        """Load template from file."""
        with open(file_path, 'r') as f:
            data = yaml.safe_load(f)
        
        return WorkflowTemplate(
            name=data.get("name"),
            version=data.get("version", "1.0"),
            description=data.get("description", ""),
            template=data.get("template", ""),
            variables=data.get("variables", {}),
            metadata=data.get("metadata", {})
        )


class TemplateRenderer:
    """Advanced template rendering with inheritance and composition."""
    
    def __init__(self):
        self.env = jinja2.Environment(
            loader=jinja2.DictLoader({}),
            trim_blocks=True,
            lstrip_blocks=True
        )
        self._add_custom_filters()
    
    def _add_custom_filters(self):
        """Add custom Jinja2 filters."""
        self.env.filters['to_yaml'] = yaml.dump
        self.env.filters['to_json'] = json.dumps
        self.env.filters['camelcase'] = lambda s: ''.join(x.title() for x in s.split('_'))
        self.env.filters['snakecase'] = lambda s: s.lower().replace(' ', '_')
    
    def render_workflow(
        self,
        base_template: str,
        includes: Optional[Dict[str, str]] = None,
        variables: Optional[Dict[str, Any]] = None
    ) -> WorkflowDefinition:
        """Render workflow with template inheritance."""
        # Add includes to loader
        if includes:
            for name, content in includes.items():
                self.env.loader.mapping[name] = content
        
        # Render template
        template = self.env.from_string(base_template)
        rendered = template.render(**(variables or {}))
        
        # Parse as workflow
        workflow_data = yaml.safe_load(rendered)
        return WorkflowDefinition.from_dict(workflow_data)
    
    def compose_workflow(
        self,
        components: List[Dict[str, Any]],
        name: str = "composed_workflow"
    ) -> WorkflowDefinition:
        """Compose workflow from components."""
        workflow_data = {
            "name": name,
            "version": "1.0",
            "states": {},
            "start_state": None,
            "end_states": []
        }
        
        # Merge components
        for component in components:
            if "states" in component:
                workflow_data["states"].update(component["states"])
            
            if "start_state" in component and not workflow_data["start_state"]:
                workflow_data["start_state"] = component["start_state"]
            
            if "end_states" in component:
                workflow_data["end_states"].extend(component["end_states"])
        
        # Remove duplicates from end_states
        workflow_data["end_states"] = list(set(workflow_data["end_states"]))
        
        return WorkflowDefinition.from_dict(workflow_data)


import json