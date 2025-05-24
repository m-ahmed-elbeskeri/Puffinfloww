"""Visual workflow editor components."""

from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
import json
import structlog

from core.workflow.node import NodeDefinition, NodeParameter
from core.workflow.flow import VisualWorkflow, NodeConnection
from core.workflow.integrations import get_registry

logger = structlog.get_logger(__name__)


@dataclass
class NodePosition:
    """Position of a node in the canvas."""
    x: float
    y: float
    
    def distance_to(self, other: "NodePosition") -> float:
        """Calculate distance to another position."""
        return ((self.x - other.x) ** 2 + (self.y - other.y) ** 2) ** 0.5


@dataclass
class NodeVisual:
    """Visual representation of a node."""
    id: str
    type: str
    position: NodePosition
    selected: bool = False
    width: float = 200
    height: float = 80
    
    def contains_point(self, x: float, y: float) -> bool:
        """Check if point is inside node."""
        return (
            self.position.x <= x <= self.position.x + self.width and
            self.position.y <= y <= self.position.y + self.height
        )
    
    def get_input_position(self, input_index: int = 0) -> Tuple[float, float]:
        """Get position of input connector."""
        return (
            self.position.x,
            self.position.y + self.height / 2 + (input_index * 20)
        )
    
    def get_output_position(self, output_index: int = 0) -> Tuple[float, float]:
        """Get position of output connector."""
        return (
            self.position.x + self.width,
            self.position.y + self.height / 2 + (output_index * 20)
        )


@dataclass
class ConnectionVisual:
    """Visual representation of a connection."""
    source_node: str
    source_output: int
    target_node: str
    target_input: int
    selected: bool = False
    
    def get_path(
        self,
        source_pos: Tuple[float, float],
        target_pos: Tuple[float, float]
    ) -> str:
        """Get SVG path for connection."""
        sx, sy = source_pos
        tx, ty = target_pos
        
        # Calculate control points for bezier curve
        dx = tx - sx
        cp1x = sx + dx * 0.5
        cp1y = sy
        cp2x = tx - dx * 0.5
        cp2y = ty
        
        return f"M {sx},{sy} C {cp1x},{cp1y} {cp2x},{cp2y} {tx},{ty}"


class NodeRenderer:
    """Renders nodes visually."""
    
    def __init__(self):
        self.registry = get_registry()
        self._node_colors = {
            "trigger": "#00AA00",
            "action": "#0066CC",
            "transform": "#FF6600",
            "condition": "#9900CC",
            "communication": "#CC0066"
        }
    
    def render_node(self, node: NodeVisual, definition: NodeDefinition) -> Dict[str, Any]:
        """Render node to visual format."""
        # Determine color based on group
        color = "#999999"
        if definition.group:
            for group in definition.group:
                if group in self._node_colors:
                    color = self._node_colors[group]
                    break
        
        if definition.icon_color:
            color = definition.icon_color
        
        return {
            "id": node.id,
            "type": node.type,
            "position": {"x": node.position.x, "y": node.position.y},
            "selected": node.selected,
            "style": {
                "width": node.width,
                "height": node.height,
                "backgroundColor": color if node.selected else f"{color}AA",
                "borderColor": color,
                "borderWidth": 2 if node.selected else 1
            },
            "content": {
                "title": definition.display_name,
                "subtitle": definition.defaults.get("name", ""),
                "icon": definition.icon,
                "inputs": len(definition.inputs),
                "outputs": len(definition.outputs)
            }
        }
    
    def render_node_palette(self) -> List[Dict[str, Any]]:
        """Render node palette."""
        palette = {}
        
        for node_info in self.registry.list_nodes():
            group = node_info["group"][0] if node_info["group"] else "other"
            
            if group not in palette:
                palette[group] = {
                    "name": group.title(),
                    "nodes": []
                }
            
            palette[group]["nodes"].append({
                "type": node_info["name"],
                "displayName": node_info["displayName"],
                "description": node_info["description"],
                "icon": node_info["icon"],
                "iconColor": node_info["iconColor"]
            })
        
        return list(palette.values())


class ConnectionRenderer:
    """Renders connections visually."""
    
    def render_connection(
        self,
        connection: ConnectionVisual,
        nodes: Dict[str, NodeVisual]
    ) -> Dict[str, Any]:
        """Render connection to visual format."""
        source_node = nodes.get(connection.source_node)
        target_node = nodes.get(connection.target_node)
        
        if not source_node or not target_node:
            return None
        
        source_pos = source_node.get_output_position(connection.source_output)
        target_pos = target_node.get_input_position(connection.target_input)
        
        return {
            "id": f"{connection.source_node}_{connection.target_node}",
            "source": connection.source_node,
            "sourceOutput": connection.source_output,
            "target": connection.target_node,
            "targetInput": connection.target_input,
            "selected": connection.selected,
            "path": ConnectionVisual(
                connection.source_node,
                connection.source_output,
                connection.target_node,
                connection.target_input
            ).get_path(source_pos, target_pos),
            "style": {
                "stroke": "#FF6600" if connection.selected else "#999999",
                "strokeWidth": 3 if connection.selected else 2
            }
        }


class WorkflowCanvas:
    """Canvas for visual workflow editing."""
    
    def __init__(self, workflow: VisualWorkflow):
        self.workflow = workflow
        self.nodes: Dict[str, NodeVisual] = {}
        self.connections: List[ConnectionVisual] = []
        self.selected_nodes: Set[str] = set()
        self.selected_connections: Set[Tuple[str, str]] = set()
        self._init_visuals()
    
    def _init_visuals(self):
        """Initialize visual representations."""
        # Create node visuals
        for node_id, node_data in self.workflow.nodes.items():
            self.nodes[node_id] = NodeVisual(
                id=node_id,
                type=node_data["type"],
                position=NodePosition(
                    node_data["position"]["x"],
                    node_data["position"]["y"]
                )
            )
        
        # Create connection visuals
        for conn in self.workflow.connections:
            self.connections.append(ConnectionVisual(
                source_node=conn.source_node,
                source_output=0,  # Would map from output name
                target_node=conn.target_node,
                target_input=0  # Would map from input name
            ))
    
    def add_node(
        self,
        node_type: str,
        x: float,
        y: float
    ) -> str:
        """Add a new node to canvas."""
        import uuid
        
        node_id = f"{node_type}_{uuid.uuid4().hex[:8]}"
        
        # Add to workflow
        self.workflow.add_node(
            node_id,
            node_type,
            {"x": x, "y": y}
        )
        
        # Add visual
        self.nodes[node_id] = NodeVisual(
            id=node_id,
            type=node_type,
            position=NodePosition(x, y)
        )
        
        return node_id
    
    def connect_nodes(
        self,
        source_node: str,
        source_output: str,
        target_node: str,
        target_input: str
    ) -> bool:
        """Connect two nodes."""
        # Validate connection
        if not self._can_connect(source_node, source_output, target_node, target_input):
            return False
        
        # Add to workflow
        self.workflow.add_connection(
            source_node,
            source_output,
            target_node,
            target_input
        )
        
        # Add visual
        self.connections.append(ConnectionVisual(
            source_node=source_node,
            source_output=0,  # Would map from name
            target_node=target_node,
            target_input=0  # Would map from name
        ))
        
        return True
    
    def _can_connect(
        self,
        source_node: str,
        source_output: str,
        target_node: str,
        target_input: str
    ) -> bool:
        """Check if connection is valid."""
        # Check nodes exist
        if source_node not in self.nodes or target_node not in self.nodes:
            return False
        
        # Check for cycles
        if self._would_create_cycle(source_node, target_node):
            return False
        
        # Check duplicate connections
        for conn in self.workflow.connections:
            if (conn.source_node == source_node and
                conn.target_node == target_node and
                conn.target_input == target_input):
                return False
        
        return True
    
    def _would_create_cycle(self, source: str, target: str) -> bool:
        """Check if connection would create a cycle."""
        # Simple DFS to check for cycles
        visited = set()
        
        def has_path(from_node: str, to_node: str) -> bool:
            if from_node == to_node:
                return True
            
            visited.add(from_node)
            
            for conn in self.workflow.connections:
                if conn.source_node == from_node and conn.target_node not in visited:
                    if has_path(conn.target_node, to_node):
                        return True
            
            return False
        
        return has_path(target, source)
    
    def select_node(self, node_id: str, multi_select: bool = False) -> None:
        """Select a node."""
        if not multi_select:
            self.clear_selection()
        
        if node_id in self.nodes:
            self.selected_nodes.add(node_id)
            self.nodes[node_id].selected = True
    
    def select_nodes_in_rect(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float
    ) -> None:
        """Select nodes within rectangle."""
        self.clear_selection()
        
        # Normalize rectangle
        min_x, max_x = min(x1, x2), max(x1, x2)
        min_y, max_y = min(y1, y2), max(y1, y2)
        
        for node_id, node in self.nodes.items():
            if (min_x <= node.position.x <= max_x and
                min_y <= node.position.y <= max_y):
                self.select_node(node_id, multi_select=True)
    
    def clear_selection(self) -> None:
        """Clear all selections."""
        for node_id in self.selected_nodes:
            if node_id in self.nodes:
                self.nodes[node_id].selected = False
        
        self.selected_nodes.clear()
        self.selected_connections.clear()
        
        for conn in self.connections:
            conn.selected = False
    
    def delete_selected(self) -> None:
        """Delete selected nodes and connections."""
        # Delete selected nodes
        for node_id in list(self.selected_nodes):
            self.workflow.remove_node(node_id)
            del self.nodes[node_id]
        
        # Update connection visuals
        self.connections = [
            conn for conn in self.connections
            if conn.source_node not in self.selected_nodes and
               conn.target_node not in self.selected_nodes
        ]
        
        self.clear_selection()
    
    def move_selected(self, dx: float, dy: float) -> None:
        """Move selected nodes."""
        for node_id in self.selected_nodes:
            if node_id in self.nodes:
                node = self.nodes[node_id]
                node.position.x += dx
                node.position.y += dy
                
                # Update workflow
                self.workflow.nodes[node_id]["position"] = {
                    "x": node.position.x,
                    "y": node.position.y
                }
    
    def auto_layout(self) -> None:
        """Auto-arrange nodes."""
        from core.dag.graph import DAG, DAGNode, DAGEdge
        
        # Build DAG
        dag = DAG("layout")
        
        for node_id in self.nodes:
            dag.add_node(DAGNode(id=node_id, data={}))
        
        for conn in self.workflow.connections:
            dag.add_edge(DAGEdge(
                source=conn.source_node,
                target=conn.target_node
            ))
        
        # Get levels
        try:
            levels = dag.get_levels()
        except:
            # If cycle exists, use simple grid
            levels = [list(self.nodes.keys())]
        
        # Arrange by levels
        x_spacing = 250
        y_spacing = 100
        x_offset = 100
        y_offset = 100
        
        for level_idx, level_nodes in enumerate(levels):
            x = x_offset + level_idx * x_spacing
            
            for node_idx, node_id in enumerate(level_nodes):
                y = y_offset + node_idx * y_spacing
                
                if node_id in self.nodes:
                    self.nodes[node_id].position = NodePosition(x, y)
                    self.workflow.nodes[node_id]["position"] = {"x": x, "y": y}


class NodeEditor:
    """Node parameter editor."""
    
    def __init__(self):
        self.registry = get_registry()
    
    def get_node_form(self, node_type: str) -> List[Dict[str, Any]]:
        """Get form definition for node parameters."""
        definition = self.registry.get_node_definition(node_type)
        if not definition:
            return []
        
        form_fields = []
        
        for param in definition.properties:
            field = {
                "name": param.name,
                "displayName": param.display_name,
                "type": param.type.value,
                "default": param.default,
                "required": param.required,
                "description": param.description,
                "placeholder": param.placeholder
            }
            
            if param.options:
                field["options"] = param.options
            
            if param.display_options:
                field["displayOptions"] = param.display_options
            
            if param.type_options:
                field["typeOptions"] = param.type_options
            
            form_fields.append(field)
        
        return form_fields
    
    def validate_parameters(
        self,
        node_type: str,
        parameters: Dict[str, Any]
    ) -> List[str]:
        """Validate node parameters."""
        definition = self.registry.get_node_definition(node_type)
        if not definition:
            return ["Unknown node type"]
        
        errors = []
        
        for param in definition.properties:
            if param.required and param.name not in parameters:
                errors.append(f"{param.display_name} is required")
            
            # Type validation
            if param.name in parameters:
                value = parameters[param.name]
                
                if param.type == ParameterType.NUMBER:
                    try:
                        float(value)
                    except:
                        errors.append(f"{param.display_name} must be a number")
                
                elif param.type == ParameterType.OPTIONS:
                    if param.options:
                        valid_values = [opt["value"] for opt in param.options]
                        if value not in valid_values:
                            errors.append(
                                f"{param.display_name} must be one of: "
                                f"{', '.join(valid_values)}"
                            )
        
        return errors


# Export functions for web UI
def export_workflow_api(workflow: VisualWorkflow) -> Dict[str, Any]:
    """Export workflow for API/UI consumption."""
    return {
        "workflow": workflow.to_dict(),
        "nodes": [
            {
                "id": node_id,
                "type": node_data["type"],
                "position": node_data["position"],
                "parameters": node_data.get("parameters", {}),
                "name": node_data.get("name", "")
            }
            for node_id, node_data in workflow.nodes.items()
        ],
        "connections": [
            {
                "source": conn.source_node,
                "sourceHandle": conn.source_output,
                "target": conn.target_node,
                "targetHandle": conn.target_input
            }
            for conn in workflow.connections
        ]
    }


from typing import Set