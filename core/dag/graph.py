"""DAG graph implementation with validation and visualization."""

from typing import Dict, List, Set, Optional, Any, Tuple, Generic, TypeVar
from dataclasses import dataclass, field
from collections import defaultdict, deque
import networkx as nx
import matplotlib.pyplot as plt
import graphviz
from datetime import datetime
import structlog

T = TypeVar('T')
logger = structlog.get_logger(__name__)


class DAGValidationError(Exception):
    """Raised when DAG validation fails."""
    pass


@dataclass
class DAGNode(Generic[T]):
    """Node in a DAG."""
    id: str
    data: T
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def __hash__(self):
        return hash(self.id)
    
    def __eq__(self, other):
        return isinstance(other, DAGNode) and self.id == other.id


@dataclass
class DAGEdge:
    """Edge in a DAG."""
    source: str
    target: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    condition: Optional[str] = None  # Optional condition expression
    
    def __hash__(self):
        return hash((self.source, self.target))


class DAG(Generic[T]):
    """Directed Acyclic Graph implementation."""
    
    def __init__(self, name: str = "dag"):
        self.name = name
        self._nodes: Dict[str, DAGNode[T]] = {}
        self._edges: Set[DAGEdge] = set()
        self._adjacency: Dict[str, Set[str]] = defaultdict(set)
        self._reverse_adjacency: Dict[str, Set[str]] = defaultdict(set)
        self._nx_graph: Optional[nx.DiGraph] = None
        self._is_validated = False
    
    def add_node(self, node: DAGNode[T]) -> None:
        """Add a node to the DAG."""
        if node.id in self._nodes:
            raise ValueError(f"Node {node.id} already exists")
        
        self._nodes[node.id] = node
        self._is_validated = False
        self._nx_graph = None
    
    def add_edge(self, edge: DAGEdge) -> None:
        """Add an edge to the DAG."""
        if edge.source not in self._nodes:
            raise ValueError(f"Source node {edge.source} not found")
        if edge.target not in self._nodes:
            raise ValueError(f"Target node {edge.target} not found")
        
        self._edges.add(edge)
        self._adjacency[edge.source].add(edge.target)
        self._reverse_adjacency[edge.target].add(edge.source)
        self._is_validated = False
        self._nx_graph = None
    
    def get_node(self, node_id: str) -> Optional[DAGNode[T]]:
        """Get a node by ID."""
        return self._nodes.get(node_id)
    
    def get_edges(self, source: Optional[str] = None, target: Optional[str] = None) -> List[DAGEdge]:
        """Get edges with optional filtering."""
        edges = []
        for edge in self._edges:
            if source and edge.source != source:
                continue
            if target and edge.target != target:
                continue
            edges.append(edge)
        return edges
    
    def get_predecessors(self, node_id: str) -> Set[str]:
        """Get immediate predecessors of a node."""
        return self._reverse_adjacency.get(node_id, set())
    
    def get_successors(self, node_id: str) -> Set[str]:
        """Get immediate successors of a node."""
        return self._adjacency.get(node_id, set())
    
    def get_ancestors(self, node_id: str) -> Set[str]:
        """Get all ancestors of a node."""
        ancestors = set()
        queue = deque(self.get_predecessors(node_id))
        
        while queue:
            node = queue.popleft()
            if node not in ancestors:
                ancestors.add(node)
                queue.extend(self.get_predecessors(node))
        
        return ancestors
    
    def get_descendants(self, node_id: str) -> Set[str]:
        """Get all descendants of a node."""
        descendants = set()
        queue = deque(self.get_successors(node_id))
        
        while queue:
            node = queue.popleft()
            if node not in descendants:
                descendants.add(node)
                queue.extend(self.get_successors(node))
        
        return descendants
    
    def validate(self) -> None:
        """Validate the DAG structure."""
        # Check for cycles
        if self.has_cycle():
            cycle = self._find_cycle()
            raise DAGValidationError(f"Cycle detected: {' -> '.join(cycle)}")
        
        # Check for disconnected components
        if self.is_disconnected():
            components = self.get_connected_components()
            raise DAGValidationError(f"DAG has {len(components)} disconnected components")
        
        self._is_validated = True
        logger.info("dag_validated", name=self.name, nodes=len(self._nodes), edges=len(self._edges))
    
    def has_cycle(self) -> bool:
        """Check if the DAG has a cycle."""
        visited = set()
        rec_stack = set()
        
        def dfs(node: str) -> bool:
            visited.add(node)
            rec_stack.add(node)
            
            for neighbor in self._adjacency.get(node, []):
                if neighbor not in visited:
                    if dfs(neighbor):
                        return True
                elif neighbor in rec_stack:
                    return True
            
            rec_stack.remove(node)
            return False
        
        for node in self._nodes:
            if node not in visited:
                if dfs(node):
                    return True
        
        return False
    
    def _find_cycle(self) -> List[str]:
        """Find a cycle in the DAG."""
        visited = set()
        rec_stack = set()
        path = []
        
        def dfs(node: str) -> Optional[List[str]]:
            visited.add(node)
            rec_stack.add(node)
            path.append(node)
            
            for neighbor in self._adjacency.get(node, []):
                if neighbor not in visited:
                    result = dfs(neighbor)
                    if result:
                        return result
                elif neighbor in rec_stack:
                    # Found cycle
                    cycle_start = path.index(neighbor)
                    return path[cycle_start:] + [neighbor]
            
            path.pop()
            rec_stack.remove(node)
            return None
        
        for node in self._nodes:
            if node not in visited:
                result = dfs(node)
                if result:
                    return result
        
        return []
    
    def is_disconnected(self) -> bool:
        """Check if the DAG has disconnected components."""
        return len(self.get_connected_components()) > 1
    
    def get_connected_components(self) -> List[Set[str]]:
        """Get connected components of the DAG."""
        visited = set()
        components = []
        
        def dfs(node: str, component: Set[str]):
            visited.add(node)
            component.add(node)
            
            # Visit successors and predecessors
            for neighbor in self._adjacency.get(node, []):
                if neighbor not in visited:
                    dfs(neighbor, component)
            
            for neighbor in self._reverse_adjacency.get(node, []):
                if neighbor not in visited:
                    dfs(neighbor, component)
        
        for node in self._nodes:
            if node not in visited:
                component = set()
                dfs(node, component)
                components.append(component)
        
        return components
    
    def topological_sort(self) -> List[str]:
        """Return nodes in topological order."""
        if not self._is_validated:
            self.validate()
        
        in_degree = defaultdict(int)
        for node in self._nodes:
            for successor in self._adjacency.get(node, []):
                in_degree[successor] += 1
        
        queue = deque([node for node in self._nodes if in_degree[node] == 0])
        result = []
        
        while queue:
            node = queue.popleft()
            result.append(node)
            
            for successor in self._adjacency.get(node, []):
                in_degree[successor] -= 1
                if in_degree[successor] == 0:
                    queue.append(successor)
        
        return result
    
    def get_levels(self) -> List[List[str]]:
        """Get nodes organized by levels (for parallel execution)."""
        if not self._is_validated:
            self.validate()
        
        in_degree = defaultdict(int)
        for node in self._nodes:
            for successor in self._adjacency.get(node, []):
                in_degree[successor] += 1
        
        levels = []
        current_level = [node for node in self._nodes if in_degree[node] == 0]
        
        while current_level:
            levels.append(current_level[:])
            next_level = []
            
            for node in current_level:
                for successor in self._adjacency.get(node, []):
                    in_degree[successor] -= 1
                    if in_degree[successor] == 0:
                        next_level.append(successor)
            
            current_level = next_level
        
        return levels
    
    def to_networkx(self) -> nx.DiGraph:
        """Convert to NetworkX graph."""
        if self._nx_graph is None:
            self._nx_graph = nx.DiGraph()
            
            # Add nodes
            for node_id, node in self._nodes.items():
                self._nx_graph.add_node(node_id, **node.metadata)
            
            # Add edges
            for edge in self._edges:
                self._nx_graph.add_edge(edge.source, edge.target, **edge.metadata)
        
        return self._nx_graph
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert DAG to dictionary representation."""
        return {
            "name": self.name,
            "nodes": {
                node_id: {
                    "data": node.data,
                    "metadata": node.metadata
                }
                for node_id, node in self._nodes.items()
            },
            "edges": [
                {
                    "source": edge.source,
                    "target": edge.target,
                    "metadata": edge.metadata,
                    "condition": edge.condition
                }
                for edge in self._edges
            ]
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any], node_factory=None) -> "DAG":
        """Create DAG from dictionary representation."""
        dag = cls(data.get("name", "dag"))
        
        # Add nodes
        for node_id, node_data in data.get("nodes", {}).items():
            if node_factory:
                node = node_factory(node_id, node_data)
            else:
                node = DAGNode(
                    id=node_id,
                    data=node_data.get("data"),
                    metadata=node_data.get("metadata", {})
                )
            dag.add_node(node)
        
        # Add edges
        for edge_data in data.get("edges", []):
            edge = DAGEdge(
                source=edge_data["source"],
                target=edge_data["target"],
                metadata=edge_data.get("metadata", {}),
                condition=edge_data.get("condition")
            )
            dag.add_edge(edge)
        
        return dag


class TopologicalSort:
    """Utilities for topological sorting."""
    
    @staticmethod
    def kahn_algorithm(dag: DAG) -> List[str]:
        """Kahn's algorithm for topological sorting."""
        return dag.topological_sort()
    
    @staticmethod
    def dfs_algorithm(dag: DAG) -> List[str]:
        """DFS-based topological sorting."""
        visited = set()
        stack = []
        
        def dfs(node: str):
            visited.add(node)
            for successor in dag.get_successors(node):
                if successor not in visited:
                    dfs(successor)
            stack.append(node)
        
        for node in dag._nodes:
            if node not in visited:
                dfs(node)
        
        return stack[::-1]
    
    @staticmethod
    def all_topological_sorts(dag: DAG) -> List[List[str]]:
        """Generate all possible topological sorts."""
        def backtrack(remaining: Set[str], current: List[str], in_degree: Dict[str, int]):
            if not remaining:
                results.append(current[:])
                return
            
            # Find nodes with in-degree 0
            available = [node for node in remaining if in_degree[node] == 0]
            
            for node in available:
                # Choose node
                current.append(node)
                remaining.remove(node)
                
                # Update in-degrees
                for successor in dag.get_successors(node):
                    in_degree[successor] -= 1
                
                # Recurse
                backtrack(remaining, current, in_degree)
                
                # Backtrack
                for successor in dag.get_successors(node):
                    in_degree[successor] += 1
                remaining.add(node)
                current.pop()
        
        # Initialize in-degrees
        in_degree = defaultdict(int)
        for node in dag._nodes:
            for successor in dag.get_successors(node):
                in_degree[successor] += 1
        
        results = []
        backtrack(set(dag._nodes.keys()), [], in_degree)
        return results


class DAGVisualizer:
    """Visualize DAG structure."""
    
    @staticmethod
    def to_dot(dag: DAG, output_file: Optional[str] = None) -> str:
        """Generate Graphviz DOT representation."""
        dot = graphviz.Digraph(dag.name)
        dot.attr(rankdir='TB')
        
        # Add nodes
        for node_id, node in dag._nodes.items():
            label = node.metadata.get("label", node_id)
            shape = node.metadata.get("shape", "box")
            color = node.metadata.get("color", "lightblue")
            
            dot.node(node_id, label=label, shape=shape, fillcolor=color, style="filled")
        
        # Add edges
        for edge in dag._edges:
            label = edge.metadata.get("label", "")
            if edge.condition:
                label = f"{label}\n[{edge.condition}]" if label else f"[{edge.condition}]"
            
            dot.edge(edge.source, edge.target, label=label)
        
        if output_file:
            dot.render(output_file, cleanup=True)
        
        return dot.source
    
    @staticmethod
    def plot_matplotlib(dag: DAG, output_file: Optional[str] = None) -> None:
        """Plot DAG using matplotlib."""
        G = dag.to_networkx()
        
        plt.figure(figsize=(12, 8))
        
        # Calculate layout
        pos = nx.spring_layout(G, k=2, iterations=50)
        
        # Draw nodes
        node_colors = [dag._nodes[node].metadata.get("color", "lightblue") for node in G.nodes()]
        nx.draw_networkx_nodes(G, pos, node_color=node_colors, node_size=1000)
        
        # Draw edges
        nx.draw_networkx_edges(G, pos, edge_color='gray', arrows=True, arrowsize=20)
        
        # Draw labels
        labels = {node: dag._nodes[node].metadata.get("label", node) for node in G.nodes()}
        nx.draw_networkx_labels(G, pos, labels, font_size=10)
        
        plt.title(f"DAG: {dag.name}")
        plt.axis('off')
        
        if output_file:
            plt.savefig(output_file, dpi=300, bbox_inches='tight')
        else:
            plt.show()
    
    @staticmethod
    def to_mermaid(dag: DAG) -> str:
        """Generate Mermaid diagram representation."""
        lines = ["graph TD"]
        
        # Add nodes
        for node_id, node in dag._nodes.items():
            label = node.metadata.get("label", node_id)
            lines.append(f"    {node_id}[{label}]")
        
        # Add edges
        for edge in dag._edges:
            arrow = "-->" if not edge.condition else f"-->|{edge.condition}|"
            lines.append(f"    {edge.source} {arrow} {edge.target}")
        
        return "\n".join(lines)