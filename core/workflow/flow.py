"""Workflow flow management and execution."""

from typing import Dict, List, Optional, Any, Set, Tuple
from dataclasses import dataclass, field
from datetime import datetime
import json
import yaml
import asyncio
import structlog
from collections import defaultdict

from core.workflow.node import (
    Node, NodeConnection, NodeData, NodeType,
    StartNode, WebhookNode, SetNode, IfNode
)
from core.workflow.integrations import get_registry
from core.dag.graph import DAG, DAGNode, DAGEdge

logger = structlog.get_logger(__name__)


@dataclass
class VisualWorkflow:
    """Visual workflow representation."""
    id: str
    name: str
    description: str = ""
    nodes: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    connections: List[NodeConnection] = field(default_factory=list)
    settings: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    
    def add_node(
        self,
        node_id: str,
        node_type: str,
        position: Dict[str, float],
        parameters: Optional[Dict[str, Any]] = None
    ) -> None:
        """Add a node to the workflow."""
        self.nodes[node_id] = {
            "type": node_type,
            "position": position,
            "parameters": parameters or {},
            "name": f"{node_type}_{len(self.nodes)}"
        }
        self.updated_at = datetime.utcnow()
    
    def add_connection(
        self,
        source_node: str,
        source_output: str,
        target_node: str,
        target_input: str
    ) -> None:
        """Add a connection between nodes."""
        connection = NodeConnection(
            source_node=source_node,
            source_output=source_output,
            target_node=target_node,
            target_input=target_input
        )
        self.connections.append(connection)
        self.updated_at = datetime.utcnow()
    
    def remove_node(self, node_id: str) -> None:
        """Remove a node and its connections."""
        if node_id in self.nodes:
            del self.nodes[node_id]
            
            # Remove related connections
            self.connections = [
                conn for conn in self.connections
                if conn.source_node != node_id and conn.target_node != node_id
            ]
            
            self.updated_at = datetime.utcnow()
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary format."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "nodes": self.nodes,
            "connections": [conn.to_dict() for conn in self.connections],
            "settings": self.settings,
            "createdAt": self.created_at.isoformat(),
            "updatedAt": self.updated_at.isoformat()
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "VisualWorkflow":
        """Create from dictionary."""
        workflow = cls(
            id=data["id"],
            name=data["name"],
            description=data.get("description", ""),
            nodes=data.get("nodes", {}),
            settings=data.get("settings", {})
        )
        
        # Parse connections
        for conn_data in data.get("connections", []):
            workflow.add_connection(
                source_node=conn_data["source"]["node"],
                source_output=conn_data["source"]["output"],
                target_node=conn_data["target"]["node"],
                target_input=conn_data["target"]["input"]
            )
        
        return workflow
    
    def to_yaml(self) -> str:
        """Export to YAML format."""
        # Convert to n8n-style YAML
        yaml_data = {
            "name": self.name,
            "nodes": []
        }
        
        # Add nodes
        for node_id, node_data in self.nodes.items():
            yaml_node = {
                "id": node_id,
                "type": node_data["type"],
                "name": node_data.get("name", node_id),
                "position": node_data["position"],
                "parameters": node_data.get("parameters", {})
            }
            yaml_data["nodes"].append(yaml_node)
        
        # Add connections
        yaml_data["connections"] = {}
        for conn in self.connections:
            source_key = f"{conn.source_node}.{conn.source_output}"
            if source_key not in yaml_data["connections"]:
                yaml_data["connections"][source_key] = []
            
            yaml_data["connections"][source_key].append({
                "node": conn.target_node,
                "input": conn.target_input
            })
        
        return yaml.dump(yaml_data, default_flow_style=False)
    
    @classmethod
    def from_yaml(cls, yaml_str: str, workflow_id: str = None) -> "VisualWorkflow":
        """Import from YAML format."""
        import uuid
        
        data = yaml.safe_load(yaml_str)
        workflow = cls(
            id=workflow_id or str(uuid.uuid4()),
            name=data.get("name", "Imported Workflow")
        )
        
        # Add nodes
        for node_data in data.get("nodes", []):
            workflow.add_node(
                node_id=node_data["id"],
                node_type=node_data["type"],
                position=node_data.get("position", {"x": 0, "y": 0}),
                parameters=node_data.get("parameters", {})
            )
        
        # Add connections
        for source_key, targets in data.get("connections", {}).items():
            source_node, source_output = source_key.split(".")
            
            for target in targets:
                workflow.add_connection(
                    source_node=source_node,
                    source_output=source_output,
                    target_node=target["node"],
                    target_input=target.get("input", "main")
                )
        
        return workflow


@dataclass
class ExecutionData:
    """Data for a single execution."""
    execution_id: str
    workflow_id: str
    status: str = "new"  # new, running, success, error
    start_time: datetime = field(default_factory=datetime.utcnow)
    end_time: Optional[datetime] = None
    node_executions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    data: Dict[str, List[List[NodeData]]] = field(default_factory=dict)
    error: Optional[str] = None
    
    def set_node_execution(
        self,
        node_id: str,
        status: str,
        data: Optional[List[List[NodeData]]] = None,
        error: Optional[str] = None
    ):
        """Record node execution."""
        self.node_executions[node_id] = {
            "status": status,
            "startTime": datetime.utcnow().isoformat(),
            "data": data,
            "error": error
        }
        
        if data:
            self.data[node_id] = data


class DataFlow:
    """Manages data flow between nodes."""
    
    def __init__(self):
        self._node_outputs: Dict[str, List[List[NodeData]]] = {}
        self._connections: List[NodeConnection] = []
    
    def set_connections(self, connections: List[NodeConnection]) -> None:
        """Set workflow connections."""
        self._connections = connections
    
    def set_node_output(
        self,
        node_id: str,
        output_data: List[List[NodeData]]
    ) -> None:
        """Set output data for a node."""
        self._node_outputs[node_id] = output_data
    
    def get_node_input(
        self,
        node_id: str
    ) -> List[List[NodeData]]:
        """Get input data for a node."""
        # Find all connections targeting this node
        input_connections = defaultdict(list)
        
        for conn in self._connections:
            if conn.target_node == node_id:
                input_connections[conn.target_input].append(conn)
        
        # Aggregate input data
        input_data = []
        
        for input_name in sorted(input_connections.keys()):
            input_items = []
            
            for conn in input_connections[input_name]:
                source_data = self._node_outputs.get(conn.source_node, [])
                
                # Get specific output
                output_index = self._get_output_index(conn.source_output)
                if output_index < len(source_data):
                    input_items.extend(source_data[output_index])
            
            input_data.append(input_items)
        
        return input_data
    
    def _get_output_index(self, output_name: str) -> int:
        """Get output index from name."""
        # Map output names to indices
        output_map = {
            "main": 0,
            "true": 0,
            "false": 1
        }
        return output_map.get(output_name, 0)


class WorkflowBuilder:
    """Build executable workflow from visual representation."""
    
    def __init__(self):
        self.registry = get_registry()
    
    def build_dag(self, workflow: VisualWorkflow) -> DAG:
        """Build DAG from visual workflow."""
        dag = DAG(workflow.name)
        
        # Add nodes
        for node_id, node_data in workflow.nodes.items():
            dag_node = DAGNode(
                id=node_id,
                data=node_data,
                metadata={
                    "type": node_data["type"],
                    "position": node_data["position"]
                }
            )
            dag.add_node(dag_node)
        
        # Add edges from connections
        for conn in workflow.connections:
            edge = DAGEdge(
                source=conn.source_node,
                target=conn.target_node,
                metadata={
                    "source_output": conn.source_output,
                    "target_input": conn.target_input
                }
            )
            dag.add_edge(edge)
        
        return dag
    
    def create_node_instances(
        self,
        workflow: VisualWorkflow
    ) -> Dict[str, Node]:
        """Create node instances from workflow."""
        nodes = {}
        
        for node_id, node_data in workflow.nodes.items():
            node_type = node_data["type"]
            
            # Get node class
            if node_type == "start":
                node_class = StartNode
            elif node_type == "webhook":
                node_class = WebhookNode
            elif node_type == "set":
                node_class = SetNode
            elif node_type == "if":
                node_class = IfNode
            else:
                node_class = self.registry.get_node_class(node_type)
            
            if node_class:
                node = node_class(
                    id=node_id,
                    parameters=node_data.get("parameters", {})
                )
                node.position = node_data["position"]
                nodes[node_id] = node
            else:
                logger.warning(
                    "unknown_node_type",
                    node_id=node_id,
                    node_type=node_type
                )
        
        return nodes
    
    def generate_code(self, workflow: VisualWorkflow) -> str:
        """Generate Python code from visual workflow."""
        code_lines = [
            f"# Generated workflow: {workflow.name}",
            f"# Generated at: {datetime.utcnow().isoformat()}",
            "",
            "import asyncio",
            "from core.workflow import WorkflowExecutor, VisualWorkflow",
            "",
            "",
            "async def main():",
            "    # Create workflow",
            f"    workflow_data = {json.dumps(workflow.to_dict(), indent=4)}",
            "    ",
            "    workflow = VisualWorkflow.from_dict(workflow_data)",
            "    ",
            "    # Create executor",
            "    executor = WorkflowExecutor()",
            "    ",
            "    # Execute workflow",
            "    result = await executor.execute(workflow)",
            "    ",
            "    print(f\"Execution {'succeeded' if result.status == 'success' else 'failed'}\")",
            "    ",
            "    # Print results",
            "    for node_id, data in result.data.items():",
            "        print(f\"Node {node_id}: {len(data[0]) if data else 0} items\")",
            "",
            "",
            "if __name__ == '__main__':",
            "    asyncio.run(main())",
        ]
        
        return "\n".join(code_lines)


class WorkflowExecutor:
    """Execute visual workflows."""
    
    def __init__(self):
        self.builder = WorkflowBuilder()
        self.data_flow = DataFlow()
    
    async def execute(
        self,
        workflow: VisualWorkflow,
        trigger_data: Optional[Dict[str, Any]] = None,
        timeout: float = 300.0
    ) -> ExecutionData:
        """Execute a workflow."""
        import uuid
        
        execution = ExecutionData(
            execution_id=str(uuid.uuid4()),
            workflow_id=workflow.id
        )
        
        try:
            execution.status = "running"
            
            # Build DAG
            dag = self.builder.build_dag(workflow)
            dag.validate()
            
            # Create node instances
            nodes = self.builder.create_node_instances(workflow)
            
            # Set connections for data flow
            self.data_flow.set_connections(workflow.connections)
            
            # Get execution order
            execution_order = dag.topological_sort()
            
            # Execute nodes in order
            context = {
                "workflow": workflow,
                "execution": execution,
                "trigger_data": trigger_data
            }
            
            for node_id in execution_order:
                if node_id not in nodes:
                    continue
                
                node = nodes[node_id]
                
                # Get input data
                input_data = self.data_flow.get_node_input(node_id)
                
                # Special handling for trigger nodes
                if isinstance(node, (StartNode, WebhookNode)) and trigger_data:
                    context["webhook_data"] = trigger_data
                
                try:
                    # Execute node
                    logger.info(
                        "executing_node",
                        node_id=node_id,
                        node_type=node.definition.name
                    )
                    
                    output_data = await asyncio.wait_for(
                        node.execute(input_data, context),
                        timeout=timeout
                    )
                    
                    # Store output
                    self.data_flow.set_node_output(node_id, output_data)
                    execution.set_node_execution(
                        node_id,
                        "success",
                        data=output_data
                    )
                    
                except Exception as e:
                    logger.error(
                        "node_execution_error",
                        node_id=node_id,
                        error=str(e)
                    )
                    
                    execution.set_node_execution(
                        node_id,
                        "error",
                        error=str(e)
                    )
                    
                    # Continue or fail based on settings
                    if workflow.settings.get("continueOnFail", False):
                        # Output empty data
                        self.data_flow.set_node_output(node_id, [[]])
                    else:
                        raise
            
            execution.status = "success"
            execution.end_time = datetime.utcnow()
            
        except Exception as e:
            execution.status = "error"
            execution.error = str(e)
            execution.end_time = datetime.utcnow()
            
            logger.error(
                "workflow_execution_error",
                workflow_id=workflow.id,
                error=str(e)
            )
        
        return execution
    
    async def test_workflow(
        self,
        workflow: VisualWorkflow,
        test_data: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Test workflow with sample data."""
        # Execute with test data
        result = await self.execute(workflow, test_data)
        
        # Analyze results
        test_result = {
            "success": result.status == "success",
            "execution_time": (
                (result.end_time - result.start_time).total_seconds()
                if result.end_time else None
            ),
            "nodes_executed": len(result.node_executions),
            "nodes_succeeded": sum(
                1 for n in result.node_executions.values()
                if n["status"] == "success"
            ),
            "nodes_failed": sum(
                1 for n in result.node_executions.values()
                if n["status"] == "error"
            ),
            "error": result.error,
            "output_data": {}
        }
        
        # Get final outputs
        for node_id, data in result.data.items():
            if data and data[0]:  # Has output
                test_result["output_data"][node_id] = [
                    item.json for item in data[0][:5]  # First 5 items
                ]
        
        return test_result


# Example usage
def create_example_workflow() -> VisualWorkflow:
    """Create an example workflow."""
    import uuid
    
    workflow = VisualWorkflow(
        id=str(uuid.uuid4()),
        name="Example ETL Workflow",
        description="Fetch data from API, transform, and send to Slack"
    )
    
    # Add nodes
    workflow.add_node(
        "start_1",
        "start",
        {"x": 100, "y": 100}
    )
    
    workflow.add_node(
        "http_1",
        "httpRequest",
        {"x": 300, "y": 100},
        {
            "method": "GET",
            "url": "https://api.example.com/data",
            "responseFormat": "json"
        }
    )
    
    workflow.add_node(
        "set_1",
        "set",
        {"x": 500, "y": 100},
        {
            "values": {
                "string": [
                    {"name": "status", "value": "processed"},
                    {"name": "timestamp", "value": "{{$now}}"}
                ]
            }
        }
    )
    
    workflow.add_node(
        "if_1",
        "if",
        {"x": 700, "y": 100},
        {
            "conditions": {
                "condition": [{
                    "value1": "{{$json.status}}",
                    "operation": "equals",
                    "value2": "processed"
                }]
            }
        }
    )
    
    workflow.add_node(
        "slack_1",
        "slack",
        {"x": 900, "y": 50},
        {
            "resource": "message",
            "operation": "send",
            "channel": "#notifications",
            "text": "Data processed successfully!"
        }
    )
    
    workflow.add_node(
        "email_1",
        "emailSend",
        {"x": 900, "y": 150},
        {
            "toEmail": "admin@example.com",
            "subject": "Processing Failed",
            "text": "The data processing workflow failed."
        }
    )
    
    # Add connections
    workflow.add_connection("start_1", "main", "http_1", "main")
    workflow.add_connection("http_1", "main", "set_1", "main")
    workflow.add_connection("set_1", "main", "if_1", "main")
    workflow.add_connection("if_1", "true", "slack_1", "main")
    workflow.add_connection("if_1", "false", "email_1", "main")
    
    return workflow


import uuid