"""Node system for n8n-style workflows."""

from typing import Dict, List, Any, Optional, Union, Callable
from dataclasses import dataclass, field
from enum import Enum
import json
import structlog
from abc import ABC, abstractmethod

logger = structlog.get_logger(__name__)


class NodeType(Enum):
    """Types of workflow nodes."""
    TRIGGER = "trigger"  # Starts workflow (webhook, cron, etc)
    ACTION = "action"    # Performs action (API call, DB query, etc)
    TRANSFORM = "transform"  # Data transformation
    CONDITION = "condition"  # Conditional branching
    LOOP = "loop"       # Iterate over items
    MERGE = "merge"     # Merge multiple inputs
    SPLIT = "split"     # Split data into multiple outputs


class ParameterType(Enum):
    """Parameter types for node configuration."""
    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"
    JSON = "json"
    OPTIONS = "options"  # Dropdown selection
    COLLECTION = "collection"  # Key-value pairs
    FIXED_COLLECTION = "fixedCollection"  # Structured data
    RESOURCE = "resource"  # Reference to external resource
    HIDDEN = "hidden"    # Hidden parameter
    NOTICE = "notice"    # Information display
    BUTTON = "button"    # Action button
    CODE = "code"        # Code editor
    COLOR = "color"      # Color picker
    DATETIME = "datetime"  # Date/time picker


@dataclass
class NodeParameter:
    """Parameter definition for node configuration."""
    name: str
    display_name: str
    type: ParameterType
    default: Any = None
    required: bool = False
    description: str = ""
    placeholder: str = ""
    options: Optional[List[Dict[str, Any]]] = None
    display_options: Optional[Dict[str, Any]] = None  # Show/hide conditions
    no_data_expression: bool = False  # Disable expressions
    type_options: Optional[Dict[str, Any]] = None  # Type-specific options
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "name": self.name,
            "displayName": self.display_name,
            "type": self.type.value,
            "default": self.default,
            "required": self.required,
            "description": self.description,
            "placeholder": self.placeholder,
            "options": self.options,
            "displayOptions": self.display_options,
            "noDataExpression": self.no_data_expression,
            "typeOptions": self.type_options
        }


@dataclass
class NodeInput:
    """Input definition for a node."""
    name: str = "main"
    display_name: str = "Main"
    type: str = "main"  # main, optional
    required: bool = True
    max_connections: Optional[int] = None
    filter: Optional[Dict[str, Any]] = None  # Filter by node type/name


@dataclass
class NodeOutput:
    """Output definition for a node."""
    name: str = "main"
    display_name: str = "Main"
    type: str = "main"  # main, optional


@dataclass
class NodeDefinition:
    """Complete node definition."""
    name: str
    display_name: str
    description: str
    group: List[str]  # Categories like ["input", "communication"]
    version: int = 1
    defaults: Dict[str, Any] = field(default_factory=dict)
    inputs: List[NodeInput] = field(default_factory=lambda: [NodeInput()])
    outputs: List[NodeOutput] = field(default_factory=lambda: [NodeOutput()])
    properties: List[NodeParameter] = field(default_factory=list)
    credentials: Optional[List[str]] = None  # Required credentials
    icon: Optional[str] = None  # Icon name or URL
    icon_color: Optional[str] = None
    subtitle: Optional[str] = None  # Dynamic subtitle
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to n8n node format."""
        return {
            "displayName": self.display_name,
            "name": self.name,
            "group": self.group,
            "version": self.version,
            "description": self.description,
            "defaults": self.defaults,
            "inputs": [{"type": i.type, "displayName": i.display_name} for i in self.inputs],
            "outputs": [{"type": o.type, "displayName": o.display_name} for o in self.outputs],
            "properties": [p.to_dict() for p in self.properties],
            "credentials": self.credentials,
            "icon": self.icon,
            "iconColor": self.icon_color
        }


@dataclass
class NodeData:
    """Data flowing through nodes."""
    json: Dict[str, Any] = field(default_factory=dict)
    binary: Optional[Dict[str, bytes]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def get(self, key: str, default: Any = None) -> Any:
        """Get value with dot notation support."""
        keys = key.split('.')
        value = self.json
        
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default
        
        return value
    
    def set(self, key: str, value: Any) -> None:
        """Set value with dot notation support."""
        keys = key.split('.')
        data = self.json
        
        for k in keys[:-1]:
            if k not in data or not isinstance(data[k], dict):
                data[k] = {}
            data = data[k]
        
        data[keys[-1]] = value
    
    def clone(self) -> "NodeData":
        """Deep clone the data."""
        return NodeData(
            json=json.loads(json.dumps(self.json)),
            binary=self.binary.copy() if self.binary else None,
            metadata=self.metadata.copy()
        )


@dataclass
class NodeConnection:
    """Connection between nodes."""
    source_node: str
    source_output: str = "main"
    target_node: str
    target_input: str = "main"
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "source": {
                "node": self.source_node,
                "output": self.source_output
            },
            "target": {
                "node": self.target_node,
                "input": self.target_input
            }
        }


class Node(ABC):
    """Base class for all workflow nodes."""
    
    def __init__(self, id: str, parameters: Dict[str, Any] = None):
        self.id = id
        self.parameters = parameters or {}
        self.position = {"x": 0, "y": 0}
        self._definition: Optional[NodeDefinition] = None
        self._execution_count = 0
    
    @property
    @abstractmethod
    def definition(self) -> NodeDefinition:
        """Get node definition."""
        pass
    
    @abstractmethod
    async def execute(
        self,
        input_data: List[List[NodeData]],
        context: Dict[str, Any]
    ) -> List[List[NodeData]]:
        """Execute the node."""
        pass
    
    def get_parameter(self, name: str, default: Any = None) -> Any:
        """Get parameter value with expression support."""
        value = self.parameters.get(name, default)
        
        # Handle expressions (simplified)
        if isinstance(value, str) and value.startswith("{{") and value.endswith("}}"):
            # This would evaluate the expression
            # For now, just return the raw value
            return value
        
        return value
    
    def validate_parameters(self) -> List[str]:
        """Validate node parameters."""
        errors = []
        
        for param in self.definition.properties:
            if param.required and param.name not in self.parameters:
                errors.append(f"Required parameter '{param.display_name}' is missing")
        
        return errors
    
    def get_node_parameter(
        self,
        parameter_name: str,
        item_index: int = 0,
        fallback_value: Any = None
    ) -> Any:
        """Get parameter value for specific item."""
        value = self.get_parameter(parameter_name, fallback_value)
        
        # Handle item-specific values
        if isinstance(value, list) and len(value) > item_index:
            return value[item_index]
        
        return value
    
    def prepare_output_data(
        self,
        output_data: Union[Dict[str, Any], List[Dict[str, Any]]],
        output_index: int = 0
    ) -> List[List[NodeData]]:
        """Prepare output data in standard format."""
        if not isinstance(output_data, list):
            output_data = [output_data]
        
        # Convert to NodeData objects
        node_data_list = []
        for item in output_data:
            if isinstance(item, NodeData):
                node_data_list.append(item)
            else:
                node_data_list.append(NodeData(json=item))
        
        # Return in n8n format: outputs[output_index][items]
        outputs = [[] for _ in range(len(self.definition.outputs))]
        outputs[output_index] = node_data_list
        
        return outputs


# Example implementations
class StartNode(Node):
    """Manual trigger node."""
    
    @property
    def definition(self) -> NodeDefinition:
        return NodeDefinition(
            name="start",
            display_name="Start",
            description="Manually trigger workflow execution",
            group=["trigger"],
            version=1,
            defaults={"name": "Start"},
            outputs=[NodeOutput()],
            icon="fa:play-circle",
            icon_color="#00AA00"
        )
    
    async def execute(
        self,
        input_data: List[List[NodeData]],
        context: Dict[str, Any]
    ) -> List[List[NodeData]]:
        """Return empty data to start workflow."""
        return self.prepare_output_data({})


class WebhookNode(Node):
    """Webhook trigger node."""
    
    @property
    def definition(self) -> NodeDefinition:
        return NodeDefinition(
            name="webhook",
            display_name="Webhook",
            description="Trigger workflow via webhook",
            group=["trigger"],
            version=1,
            defaults={"name": "Webhook"},
            outputs=[NodeOutput()],
            properties=[
                NodeParameter(
                    name="httpMethod",
                    display_name="HTTP Method",
                    type=ParameterType.OPTIONS,
                    default="GET",
                    options=[
                        {"name": "GET", "value": "GET"},
                        {"name": "POST", "value": "POST"},
                        {"name": "PUT", "value": "PUT"},
                        {"name": "DELETE", "value": "DELETE"}
                    ]
                ),
                NodeParameter(
                    name="path",
                    display_name="Path",
                    type=ParameterType.STRING,
                    default="webhook",
                    required=True,
                    placeholder="webhook-path"
                ),
                NodeParameter(
                    name="responseMode",
                    display_name="Response Mode",
                    type=ParameterType.OPTIONS,
                    default="onReceived",
                    options=[
                        {"name": "On Received", "value": "onReceived"},
                        {"name": "Last Node", "value": "lastNode"}
                    ]
                )
            ],
            icon="fa:webhook",
            icon_color="#885577"
        )
    
    async def execute(
        self,
        input_data: List[List[NodeData]],
        context: Dict[str, Any]
    ) -> List[List[NodeData]]:
        """Return webhook data."""
        webhook_data = context.get("webhook_data", {})
        return self.prepare_output_data(webhook_data)


class SetNode(Node):
    """Set/transform data node."""
    
    @property
    def definition(self) -> NodeDefinition:
        return NodeDefinition(
            name="set",
            display_name="Set",
            description="Set data values",
            group=["transform"],
            version=1,
            defaults={"name": "Set", "keepOnlySet": False},
            inputs=[NodeInput()],
            outputs=[NodeOutput()],
            properties=[
                NodeParameter(
                    name="keepOnlySet",
                    display_name="Keep Only Set",
                    type=ParameterType.BOOLEAN,
                    default=False,
                    description="Keep only the values set in this node"
                ),
                NodeParameter(
                    name="values",
                    display_name="Values to Set",
                    type=ParameterType.FIXED_COLLECTION,
                    default={},
                    type_options={
                        "multipleValues": True,
                        "options": [
                            {
                                "name": "string",
                                "displayName": "String",
                                "values": [
                                    {
                                        "displayName": "Name",
                                        "name": "name",
                                        "type": "string",
                                        "default": ""
                                    },
                                    {
                                        "displayName": "Value",
                                        "name": "value",
                                        "type": "string",
                                        "default": ""
                                    }
                                ]
                            },
                            {
                                "name": "number",
                                "displayName": "Number",
                                "values": [
                                    {
                                        "displayName": "Name",
                                        "name": "name",
                                        "type": "string",
                                        "default": ""
                                    },
                                    {
                                        "displayName": "Value",
                                        "name": "value",
                                        "type": "number",
                                        "default": 0
                                    }
                                ]
                            },
                            {
                                "name": "boolean",
                                "displayName": "Boolean",
                                "values": [
                                    {
                                        "displayName": "Name",
                                        "name": "name",
                                        "type": "string",
                                        "default": ""
                                    },
                                    {
                                        "displayName": "Value",
                                        "name": "value",
                                        "type": "boolean",
                                        "default": True
                                    }
                                ]
                            }
                        ]
                    }
                )
            ],
            icon="fa:pen-square",
            icon_color="#0000FF"
        )
    
    async def execute(
        self,
        input_data: List[List[NodeData]],
        context: Dict[str, Any]
    ) -> List[List[NodeData]]:
        """Set values in data."""
        keep_only_set = self.get_parameter("keepOnlySet", False)
        values = self.get_parameter("values", {})
        
        output_data = []
        
        # Get input items
        items = input_data[0] if input_data else []
        
        for item in items:
            if keep_only_set:
                new_item = NodeData()
            else:
                new_item = item.clone()
            
            # Set values
            for value_type, value_list in values.items():
                if not isinstance(value_list, list):
                    continue
                    
                for value_def in value_list:
                    name = value_def.get("name")
                    value = value_def.get("value")
                    
                    if name:
                        new_item.set(name, value)
            
            output_data.append(new_item)
        
        return self.prepare_output_data(output_data)


class IfNode(Node):
    """Conditional branching node."""
    
    @property
    def definition(self) -> NodeDefinition:
        return NodeDefinition(
            name="if",
            display_name="IF",
            description="Route items based on conditions",
            group=["condition"],
            version=1,
            defaults={"name": "IF"},
            inputs=[NodeInput()],
            outputs=[
                NodeOutput(name="true", display_name="True"),
                NodeOutput(name="false", display_name="False")
            ],
            properties=[
                NodeParameter(
                    name="conditions",
                    display_name="Conditions",
                    type=ParameterType.FIXED_COLLECTION,
                    default={},
                    type_options={
                        "multipleValues": True,
                        "options": [{
                            "name": "condition",
                            "displayName": "Condition",
                            "values": [
                                {
                                    "displayName": "Value 1",
                                    "name": "value1",
                                    "type": "string",
                                    "default": ""
                                },
                                {
                                    "displayName": "Operation",
                                    "name": "operation",
                                    "type": "options",
                                    "options": [
                                        {"name": "Equals", "value": "equals"},
                                        {"name": "Not Equals", "value": "notEquals"},
                                        {"name": "Contains", "value": "contains"},
                                        {"name": "Greater Than", "value": "gt"},
                                        {"name": "Less Than", "value": "lt"}
                                    ],
                                    "default": "equals"
                                },
                                {
                                    "displayName": "Value 2",
                                    "name": "value2",
                                    "type": "string",
                                    "default": ""
                                }
                            ]
                        }]
                    }
                ),
                NodeParameter(
                    name="combineOperation",
                    display_name="Combine",
                    type=ParameterType.OPTIONS,
                    default="all",
                    options=[
                        {"name": "ALL", "value": "all", "description": "All conditions must match"},
                        {"name": "ANY", "value": "any", "description": "Any condition must match"}
                    ]
                )
            ],
            icon="fa:code-branch",
            icon_color="#FF6600"
        )
    
    async def execute(
        self,
        input_data: List[List[NodeData]],
        context: Dict[str, Any]
    ) -> List[List[NodeData]]:
        """Evaluate conditions and route items."""
        conditions = self.get_parameter("conditions", {}).get("condition", [])
        combine = self.get_parameter("combineOperation", "all")
        
        true_items = []
        false_items = []
        
        items = input_data[0] if input_data else []
        
        for item in items:
            results = []
            
            for condition in conditions:
                value1 = self._resolve_value(condition.get("value1", ""), item)
                value2 = self._resolve_value(condition.get("value2", ""), item)
                operation = condition.get("operation", "equals")
                
                result = self._evaluate_condition(value1, operation, value2)
                results.append(result)
            
            # Combine results
            if combine == "all":
                matches = all(results) if results else False
            else:  # any
                matches = any(results) if results else False
            
            if matches:
                true_items.append(item)
            else:
                false_items.append(item)
        
        return [true_items, false_items]
    
    def _resolve_value(self, value: str, item: NodeData) -> Any:
        """Resolve value from expression or literal."""
        if isinstance(value, str) and value.startswith("{{") and value.endswith("}}"):
            # Extract property path
            expr = value[2:-2].strip()
            if expr.startswith("$json."):
                path = expr[6:]
                return item.get(path)
        return value
    
    def _evaluate_condition(self, value1: Any, operation: str, value2: Any) -> bool:
        """Evaluate a single condition."""
        if operation == "equals":
            return value1 == value2
        elif operation == "notEquals":
            return value1 != value2
        elif operation == "contains":
            return str(value2) in str(value1)
        elif operation == "gt":
            try:
                return float(value1) > float(value2)
            except:
                return False
        elif operation == "lt":
            try:
                return float(value1) < float(value2)
            except:
                return False
        return False