"""YAML parser for workflow definitions."""

import yaml
from typing import Dict, List, Optional, Any, Union
from dataclasses import dataclass, field
from pathlib import Path
import json
import structlog
from datetime import datetime

logger = structlog.get_logger(__name__)


@dataclass
class ResourceDefinition:
    """Resource requirements definition."""
    cpu_units: float = 1.0
    memory_mb: float = 100.0
    io_weight: float = 1.0
    network_weight: float = 1.0
    gpu_units: float = 0.0
    timeout: Optional[float] = None
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ResourceDefinition":
        """Create from dictionary."""
        return cls(
            cpu_units=data.get("cpu", 1.0),
            memory_mb=data.get("memory", 100.0),
            io_weight=data.get("io", 1.0),
            network_weight=data.get("network", 1.0),
            gpu_units=data.get("gpu", 0.0),
            timeout=data.get("timeout")
        )


@dataclass
class TransitionDefinition:
    """State transition definition."""
    target: str
    condition: Optional[str] = None
    probability: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    @classmethod
    def from_dict(cls, data: Union[str, Dict[str, Any]]) -> "TransitionDefinition":
        """Create from dictionary or string."""
        if isinstance(data, str):
            return cls(target=data)
        
        return cls(
            target=data["target"],
            condition=data.get("condition"),
            probability=data.get("probability"),
            metadata=data.get("metadata", {})
        )


@dataclass
class StateDefinition:
    """State definition in workflow."""
    name: str
    type: str
    config: Dict[str, Any] = field(default_factory=dict)
    dependencies: List[str] = field(default_factory=list)
    transitions: List[TransitionDefinition] = field(default_factory=list)
    resources: Optional[ResourceDefinition] = None
    retries: int = 3
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    @classmethod
    def from_dict(cls, name: str, data: Dict[str, Any]) -> "StateDefinition":
        """Create from dictionary."""
        # Parse transitions
        transitions = []
        if "transitions" in data:
            for trans in data["transitions"]:
                transitions.append(TransitionDefinition.from_dict(trans))
        elif "next" in data:
            # Simple next state
            if isinstance(data["next"], list):
                for next_state in data["next"]:
                    transitions.append(TransitionDefinition.from_dict(next_state))
            else:
                transitions.append(TransitionDefinition.from_dict(data["next"]))
        
        # Parse resources
        resources = None
        if "resources" in data:
            resources = ResourceDefinition.from_dict(data["resources"])
        
        return cls(
            name=name,
            type=data.get("type", "task"),
            config=data.get("config", {}),
            dependencies=data.get("dependencies", []),
            transitions=transitions,
            resources=resources,
            retries=data.get("retries", 3),
            metadata=data.get("metadata", {})
        )


@dataclass
class WorkflowDefinition:
    """Complete workflow definition."""
    name: str
    version: str = "1.0"
    description: str = ""
    states: Dict[str, StateDefinition] = field(default_factory=dict)
    start_state: Optional[str] = None
    end_states: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorkflowDefinition":
        """Create from dictionary."""
        # Parse states
        states = {}
        for state_name, state_data in data.get("states", {}).items():
            states[state_name] = StateDefinition.from_dict(state_name, state_data)
        
        return cls(
            name=data.get("name", "workflow"),
            version=data.get("version", "1.0"),
            description=data.get("description", ""),
            states=states,
            start_state=data.get("start_state"),
            end_states=data.get("end_states", []),
            metadata=data.get("metadata", {})
        )


class YAMLParser:
    """Parse YAML workflow definitions."""
    
    @staticmethod
    def parse_file(file_path: Union[str, Path]) -> WorkflowDefinition:
        """Parse workflow from YAML file."""
        file_path = Path(file_path)
        
        if not file_path.exists():
            raise FileNotFoundError(f"Workflow file not found: {file_path}")
        
        with open(file_path, 'r') as f:
            data = yaml.safe_load(f)
        
        return YAMLParser.parse_dict(data)
    
    @staticmethod
    def parse_string(yaml_string: str) -> WorkflowDefinition:
        """Parse workflow from YAML string."""
        data = yaml.safe_load(yaml_string)
        return YAMLParser.parse_dict(data)
    
    @staticmethod
    def parse_dict(data: Dict[str, Any]) -> WorkflowDefinition:
        """Parse workflow from dictionary."""
        workflow = WorkflowDefinition.from_dict(data)
        
        # Validate
        YAMLParser.validate_workflow(workflow)
        
        logger.info(
            "workflow_parsed",
            name=workflow.name,
            version=workflow.version,
            states=len(workflow.states)
        )
        
        return workflow
    
    @staticmethod
    def validate_workflow(workflow: WorkflowDefinition) -> None:
        """Validate workflow definition."""
        # Check start state exists
        if workflow.start_state and workflow.start_state not in workflow.states:
            raise ValueError(f"Start state '{workflow.start_state}' not found in states")
        
        # Check end states exist
        for end_state in workflow.end_states:
            if end_state not in workflow.states:
                raise ValueError(f"End state '{end_state}' not found in states")
        
        # Check all transitions point to valid states
        for state_name, state in workflow.states.items():
            for transition in state.transitions:
                if transition.target not in workflow.states:
                    raise ValueError(
                        f"State '{state_name}' has transition to unknown state '{transition.target}'"
                    )
        
        # Check dependencies
        for state_name, state in workflow.states.items():
            for dep in state.dependencies:
                if dep not in workflow.states:
                    raise ValueError(
                        f"State '{state_name}' has dependency on unknown state '{dep}'"
                    )
    
    @staticmethod
    def to_yaml(workflow: WorkflowDefinition) -> str:
        """Convert workflow definition to YAML."""
        data = {
            "name": workflow.name,
            "version": workflow.version,
            "description": workflow.description,
            "start_state": workflow.start_state,
            "end_states": workflow.end_states,
            "metadata": workflow.metadata,
            "states": {}
        }
        
        # Convert states
        for state_name, state in workflow.states.items():
            state_data = {
                "type": state.type,
                "config": state.config,
                "dependencies": state.dependencies,
                "retries": state.retries,
                "metadata": state.metadata
            }
            
            # Add transitions
            if state.transitions:
                state_data["transitions"] = []
                for trans in state.transitions:
                    if trans.condition or trans.probability or trans.metadata:
                        trans_data = {
                            "target": trans.target,
                            "condition": trans.condition,
                            "probability": trans.probability,
                            "metadata": trans.metadata
                        }
                        state_data["transitions"].append(trans_data)
                    else:
                        state_data["transitions"].append(trans.target)
            
            # Add resources
            if state.resources:
                state_data["resources"] = {
                    "cpu": state.resources.cpu_units,
                    "memory": state.resources.memory_mb,
                    "io": state.resources.io_weight,
                    "network": state.resources.network_weight,
                    "gpu": state.resources.gpu_units,
                    "timeout": state.resources.timeout
                }
            
            data["states"][state_name] = state_data
        
        return yaml.dump(data, default_flow_style=False, sort_keys=False)


# Example YAML workflow
EXAMPLE_WORKFLOW_YAML = """
name: data_processing_pipeline
version: "1.0"
description: "Example data processing workflow"
start_state: fetch_data
end_states: [notify_complete]

metadata:
  author: "system"
  tags: ["data", "etl"]

states:
  fetch_data:
    type: task
    config:
      plugin: "http"
      url: "https://api.example.com/data"
      method: "GET"
    transitions:
      - validate_data
    resources:
      cpu: 1
      memory: 512
      timeout: 300
    retries: 3

  validate_data:
    type: task
    config:
      plugin: "validation"
      schema: "data_schema.json"
    dependencies: [fetch_data]
    transitions:
      - target: process_data
        condition: "valid == true"
      - target: handle_error
        condition: "valid == false"
    resources:
      cpu: 2
      memory: 1024

  process_data:
    type: parallel
    config:
      tasks:
        - transform_data
        - analyze_data
    dependencies: [validate_data]
    transitions:
      - aggregate_results

  transform_data:
    type: task
    config:
      plugin: "transform"
      operations: ["normalize", "enrich"]
    resources:
      cpu: 4
      memory: 2048

  analyze_data:
    type: task
    config:
      plugin: "analytics"
      metrics: ["mean", "std", "correlation"]
    resources:
      cpu: 2
      memory: 2048
      gpu: 1

  aggregate_results:
    type: task
    config:
      plugin: "aggregator"
    dependencies: [transform_data, analyze_data]
    transitions:
      - store_results

  store_results:
    type: task
    config:
      plugin: "storage"
      backend: "s3"
      bucket: "results"
    dependencies: [aggregate_results]
    transitions:
      - notify_complete

  handle_error:
    type: task
    config:
      plugin: "error_handler"
      action: "log_and_alert"
    transitions:
      - notify_complete

  notify_complete:
    type: task
    config:
      plugin: "notification"
      channels: ["email", "slack"]
    dependencies: [store_results, handle_error]
"""