"""Determinism checking and SHA256 pinning for workflow execution."""

import hashlib
import json
import inspect
import dis
from typing import Dict, Any, Optional, Callable, Set, List, Tuple
from dataclasses import dataclass, field
from datetime import datetime
import ast
import asyncio
from functools import wraps
import structlog

from core.agent.base import Agent, StateFunction
from core.agent.context import Context


logger = structlog.get_logger(__name__)


class NonDeterministicError(Exception):
    """Raised when non-deterministic behavior is detected."""
    pass


@dataclass
class StateFingerprint:
    """Fingerprint of a state function for determinism checking."""
    state_name: str
    function_hash: str
    source_hash: str
    dependencies: Set[str] = field(default_factory=set)
    imports: Set[str] = field(default_factory=set)
    external_calls: Set[str] = field(default_factory=set)
    timestamp: datetime = field(default_factory=datetime.utcnow)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "state_name": self.state_name,
            "function_hash": self.function_hash,
            "source_hash": self.source_hash,
            "dependencies": list(self.dependencies),
            "imports": list(self.imports),
            "external_calls": list(self.external_calls),
            "timestamp": self.timestamp.isoformat()
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StateFingerprint":
        """Create from dictionary."""
        return cls(
            state_name=data["state_name"],
            function_hash=data["function_hash"],
            source_hash=data["source_hash"],
            dependencies=set(data.get("dependencies", [])),
            imports=set(data.get("imports", [])),
            external_calls=set(data.get("external_calls", [])),
            timestamp=datetime.fromisoformat(data["timestamp"])
        )


class FunctionAnalyzer:
    """Analyzes functions for determinism."""
    
    # Known non-deterministic functions
    NON_DETERMINISTIC_CALLS = {
        "random", "randint", "choice", "shuffle",
        "time", "datetime", "now", "utcnow",
        "uuid", "uuid4",
        "input", "print",
        "open", "read", "write"
    }
    
    # Safe built-ins
    SAFE_BUILTINS = {
        "len", "str", "int", "float", "bool",
        "list", "dict", "set", "tuple",
        "min", "max", "sum", "abs",
        "all", "any", "enumerate", "range",
        "sorted", "reversed"
    }
    
    def analyze_function(self, func: Callable) -> StateFingerprint:
        """Analyze a function for determinism."""
        # Get source code
        try:
            source = inspect.getsource(func)
        except OSError:
            # Built-in or compiled function
            source = ""
        
        # Calculate hashes
        function_hash = self._hash_bytecode(func)
        source_hash = hashlib.sha256(source.encode()).hexdigest()
        
        # Parse AST
        try:
            tree = ast.parse(source)
        except SyntaxError:
            tree = None
        
        # Extract dependencies
        dependencies = set()
        imports = set()
        external_calls = set()
        
        if tree:
            visitor = DeterminismVisitor()
            visitor.visit(tree)
            
            dependencies = visitor.dependencies
            imports = visitor.imports
            external_calls = visitor.external_calls
        
        return StateFingerprint(
            state_name=func.__name__,
            function_hash=function_hash,
            source_hash=source_hash,
            dependencies=dependencies,
            imports=imports,
            external_calls=external_calls
        )
    
    def _hash_bytecode(self, func: Callable) -> str:
        """Hash function bytecode."""
        if asyncio.iscoroutinefunction(func):
            # For async functions, hash the code object
            code = func.__code__
        else:
            code = func.__code__
        
        # Create stable hash from code attributes
        code_data = {
            "co_code": code.co_code.hex(),
            "co_consts": str(code.co_consts),
            "co_names": code.co_names,
            "co_varnames": code.co_varnames,
            "co_argcount": code.co_argcount
        }
        
        return hashlib.sha256(
            json.dumps(code_data, sort_keys=True).encode()
        ).hexdigest()
    
    def check_determinism(self, func: Callable) -> List[str]:
        """Check function for non-deterministic patterns."""
        issues = []
        
        # Get source
        try:
            source = inspect.getsource(func)
            tree = ast.parse(source)
        except (OSError, SyntaxError):
            return ["Cannot analyze function source"]
        
        # Check for non-deterministic calls
        visitor = DeterminismVisitor()
        visitor.visit(tree)
        
        for call in visitor.external_calls:
            if any(nd in call for nd in self.NON_DETERMINISTIC_CALLS):
                issues.append(f"Non-deterministic call detected: {call}")
        
        # Check for I/O operations
        if visitor.has_io:
            issues.append("I/O operations detected")
        
        # Check for global state access
        if visitor.accesses_globals:
            issues.append("Global state access detected")
        
        return issues


class DeterminismVisitor(ast.NodeVisitor):
    """AST visitor for determinism analysis."""
    
    def __init__(self):
        self.dependencies = set()
        self.imports = set()
        self.external_calls = set()
        self.has_io = False
        self.accesses_globals = False
    
    def visit_Import(self, node: ast.Import):
        """Visit import statement."""
        for alias in node.names:
            self.imports.add(alias.name)
        self.generic_visit(node)
    
    def visit_ImportFrom(self, node: ast.ImportFrom):
        """Visit from-import statement."""
        if node.module:
            self.imports.add(node.module)
        self.generic_visit(node)
    
    def visit_Call(self, node: ast.Call):
        """Visit function call."""
        if isinstance(node.func, ast.Name):
            self.external_calls.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Name):
                call_name = f"{node.func.value.id}.{node.func.attr}"
                self.external_calls.add(call_name)
                
                # Check for I/O
                if node.func.value.id in {"open", "file"}:
                    self.has_io = True
        
        self.generic_visit(node)
    
    def visit_Global(self, node: ast.Global):
        """Visit global statement."""
        self.accesses_globals = True
        self.generic_visit(node)
    
    def visit_Name(self, node: ast.Name):
        """Visit name reference."""
        if isinstance(node.ctx, ast.Load):
            self.dependencies.add(node.id)
        self.generic_visit(node)


class DeterminismChecker:
    """Checks and enforces determinism in workflow execution."""
    
    def __init__(self):
        self.analyzer = FunctionAnalyzer()
        self._fingerprints: Dict[str, StateFingerprint] = {}
        self._execution_history: List[Tuple[str, Dict[str, Any]]] = []
        self._state_outputs: Dict[str, List[Any]] = {}
    
    def register_state(self, state_name: str, func: StateFunction) -> StateFingerprint:
        """Register a state function and compute its fingerprint."""
        fingerprint = self.analyzer.analyze_function(func)
        fingerprint.state_name = state_name
        self._fingerprints[state_name] = fingerprint
        
        # Check for determinism issues
        issues = self.analyzer.check_determinism(func)
        if issues:
            logger.warning(
                "determinism_issues_found",
                state_name=state_name,
                issues=issues
            )
        
        return fingerprint
    
    def validate_fingerprint(
        self,
        state_name: str,
        expected: StateFingerprint
    ) -> bool:
        """Validate state fingerprint matches expected."""
        current = self._fingerprints.get(state_name)
        if not current:
            return False
        
        # Compare hashes
        if current.function_hash != expected.function_hash:
            logger.error(
                "fingerprint_mismatch",
                state_name=state_name,
                expected_hash=expected.function_hash,
                current_hash=current.function_hash
            )
            return False
        
        # Check for new external calls
        new_calls = current.external_calls - expected.external_calls
        if new_calls:
            logger.warning(
                "new_external_calls",
                state_name=state_name,
                new_calls=list(new_calls)
            )
        
        return True
    
    def check_state_start(
        self,
        state_name: str,
        inputs: Dict[str, Any]
    ) -> None:
        """Check determinism at state start."""
        # Record execution
        self._execution_history.append((state_name, inputs))
        
        # Hash inputs for comparison
        input_hash = self._hash_inputs(inputs)
        
        logger.debug(
            "state_start_recorded",
            state_name=state_name,
            input_hash=input_hash
        )
    
    def check_state_completion(
        self,
        state_name: str,
        outputs: Dict[str, Any]
    ) -> None:
        """Check determinism at state completion."""
        # Record outputs
        if state_name not in self._state_outputs:
            self._state_outputs[state_name] = []
        
        self._state_outputs[state_name].append(outputs)
        
        # If we've seen this state before with same inputs, compare outputs
        if len(self._state_outputs[state_name]) > 1:
            previous = self._state_outputs[state_name][-2]
            if not self._outputs_match(previous, outputs):
                raise NonDeterministicError(
                    f"State '{state_name}' produced different outputs for same inputs"
                )
    
    def _hash_inputs(self, inputs: Dict[str, Any]) -> str:
        """Create deterministic hash of inputs."""
        # Convert to stable JSON representation
        stable_json = json.dumps(
            inputs,
            sort_keys=True,
            default=str  # Convert non-serializable objects to strings
        )
        return hashlib.sha256(stable_json.encode()).hexdigest()
    
    def _outputs_match(self, out1: Dict[str, Any], out2: Dict[str, Any]) -> bool:
        """Check if two output dictionaries match."""
        # Simple comparison - could be made more sophisticated
        return json.dumps(out1, sort_keys=True) == json.dumps(out2, sort_keys=True)
    
    def validate_agent_state(self, agent: Agent) -> None:
        """Validate agent state for determinism."""
        # Check all registered states
        for state_name, func in agent.states.items():
            if state_name not in self._fingerprints:
                self.register_state(state_name, func)
        
        # Validate execution order
        if hasattr(agent, "_execution_order"):
            # This would check that states execute in deterministic order
            pass
    
    def create_execution_manifest(self, agent: Agent) -> Dict[str, Any]:
        """Create manifest of execution for validation."""
        manifest = {
            "agent_name": agent.name,
            "timestamp": datetime.utcnow().isoformat(),
            "fingerprints": {},
            "execution_stats": {
                "total_states": len(agent.states),
                "completed_states": len(agent.completed_states),
                "execution_count": len(self._execution_history)
            }
        }
        
        # Add all fingerprints
        for state_name, func in agent.states.items():
            fingerprint = self.register_state(state_name, func)
            manifest["fingerprints"][state_name] = fingerprint.to_dict()
        
        return manifest
    
    def load_manifest(self, manifest: Dict[str, Any]) -> None:
        """Load execution manifest for validation."""
        # Load fingerprints
        for state_name, fp_data in manifest.get("fingerprints", {}).items():
            self._fingerprints[state_name] = StateFingerprint.from_dict(fp_data)


def deterministic(func: Callable) -> Callable:
    """
    Decorator to mark and validate deterministic functions.
    
    Example:
        @deterministic
        async def process_data(context: Context):
            data = context.get_state("data")
            result = sorted(data)  # Deterministic operation
            return result
    """
    # Create analyzer
    analyzer = FunctionAnalyzer()
    
    # Check function
    issues = analyzer.check_determinism(func)
    if issues:
        logger.warning(
            "deterministic_decorator_issues",
            function=func.__name__,
            issues=issues
        )
    
    # Create wrapper
    if asyncio.iscoroutinefunction(func):
        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            # Could add runtime determinism checks here
            result = await func(*args, **kwargs)
            return result
        return async_wrapper
    else:
        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            # Could add runtime determinism checks here
            result = func(*args, **kwargs)
            return result
        return sync_wrapper


def capture_state(key: str) -> Callable:
    """
    Decorator to capture state for determinism validation.
    
    Example:
        @capture_state("user_processing")
        async def process_user(context: Context):
            # Function implementation
            pass
    """
    def decorator(func: Callable) -> Callable:
        if asyncio.iscoroutinefunction(func):
            @wraps(func)
            async def async_wrapper(*args, **kwargs):
                # Capture input state
                context = args[0] if args and isinstance(args[0], Context) else None
                if context:
                    input_state = {
                        k: v for k, v in context._state_data.items()
                    }
                    logger.debug(
                        "state_captured",
                        key=key,
                        input_keys=list(input_state.keys())
                    )
                
                # Execute function
                result = await func(*args, **kwargs)
                
                # Capture output state
                if context:
                    output_state = {
                        k: v for k, v in context._state_data.items()
                    }
                    logger.debug(
                        "state_captured",
                        key=key,
                        output_keys=list(output_state.keys())
                    )
                
                return result
            return async_wrapper
        else:
            @wraps(func)
            def sync_wrapper(*args, **kwargs):
                # Similar logic for sync functions
                result = func(*args, **kwargs)
                return result
            return sync_wrapper
    return decorator