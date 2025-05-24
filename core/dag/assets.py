"""Asset management for DAG workflows (Dagster-style)."""

from typing import Dict, List, Optional, Any, Set, Union
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import hashlib
import structlog
from enum import Enum

logger = structlog.get_logger(__name__)


class AssetMaterialization(Enum):
    """Asset materialization status."""
    PLANNED = "planned"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    STALE = "stale"


@dataclass
class AssetMetadata:
    """Metadata for an asset."""
    schema: Optional[Dict[str, Any]] = None
    row_count: Optional[int] = None
    size_bytes: Optional[int] = None
    format: Optional[str] = None
    compression: Optional[str] = None
    tags: Set[str] = field(default_factory=set)
    custom: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Asset:
    """Data asset produced by workflow states."""
    key: str
    description: str = ""
    path: Optional[str] = None
    producing_state: Optional[str] = None
    consuming_states: Set[str] = field(default_factory=set)
    partitions: Optional[List[str]] = None
    metadata: AssetMetadata = field(default_factory=AssetMetadata)
    materialization_status: AssetMaterialization = AssetMaterialization.PLANNED
    last_materialized: Optional[datetime] = None
    version: Optional[str] = None
    
    def __hash__(self):
        return hash(self.key)
    
    def get_partition_key(self, partition: str) -> str:
        """Get key for specific partition."""
        return f"{self.key}:{partition}"
    
    def is_stale(self, upstream_assets: List["Asset"]) -> bool:
        """Check if asset is stale based on upstream changes."""
        if not self.last_materialized:
            return True
        
        for upstream in upstream_assets:
            if upstream.last_materialized and upstream.last_materialized > self.last_materialized:
                return True
        
        return False
    
    def compute_version(self, inputs: Dict[str, Any]) -> str:
        """Compute version hash based on inputs."""
        # Create deterministic hash
        hasher = hashlib.sha256()
        
        # Add producing state
        if self.producing_state:
            hasher.update(self.producing_state.encode())
        
        # Add input data
        for key in sorted(inputs.keys()):
            hasher.update(f"{key}:{inputs[key]}".encode())
        
        return hasher.hexdigest()[:16]


@dataclass
class AssetLineage:
    """Lineage information for assets."""
    asset_key: str
    upstream_assets: Set[str] = field(default_factory=set)
    downstream_assets: Set[str] = field(default_factory=set)
    transformations: List[str] = field(default_factory=list)
    
    def add_upstream(self, asset_key: str):
        """Add upstream dependency."""
        self.upstream_assets.add(asset_key)
    
    def add_downstream(self, asset_key: str):
        """Add downstream dependency."""
        self.downstream_assets.add(asset_key)
    
    def get_full_lineage(self, catalog: "AssetCatalog") -> Dict[str, Set[str]]:
        """Get complete lineage graph."""
        lineage = {
            "upstream": set(),
            "downstream": set()
        }
        
        # Traverse upstream
        queue = list(self.upstream_assets)
        visited = set()
        
        while queue:
            asset_key = queue.pop(0)
            if asset_key in visited:
                continue
            
            visited.add(asset_key)
            lineage["upstream"].add(asset_key)
            
            # Get upstream of this asset
            asset_lineage = catalog.get_lineage(asset_key)
            if asset_lineage:
                queue.extend(asset_lineage.upstream_assets)
        
        # Traverse downstream
        queue = list(self.downstream_assets)
        visited = set()
        
        while queue:
            asset_key = queue.pop(0)
            if asset_key in visited:
                continue
            
            visited.add(asset_key)
            lineage["downstream"].add(asset_key)
            
            # Get downstream of this asset
            asset_lineage = catalog.get_lineage(asset_key)
            if asset_lineage:
                queue.extend(asset_lineage.downstream_assets)
        
        return lineage


class AssetCatalog:
    """Catalog of all assets in the system."""
    
    def __init__(self):
        self._assets: Dict[str, Asset] = {}
        self._lineage: Dict[str, AssetLineage] = {}
        self._state_assets: Dict[str, Set[str]] = {}  # state -> assets produced
    
    def register_asset(self, asset: Asset) -> None:
        """Register an asset in the catalog."""
        self._assets[asset.key] = asset
        
        # Initialize lineage
        if asset.key not in self._lineage:
            self._lineage[asset.key] = AssetLineage(asset_key=asset.key)
        
        # Track by producing state
        if asset.producing_state:
            if asset.producing_state not in self._state_assets:
                self._state_assets[asset.producing_state] = set()
            self._state_assets[asset.producing_state].add(asset.key)
        
        logger.info(
            "asset_registered",
            asset_key=asset.key,
            producing_state=asset.producing_state
        )
    
    def get_asset(self, key: str) -> Optional[Asset]:
        """Get asset by key."""
        return self._assets.get(key)
    
    def get_lineage(self, key: str) -> Optional[AssetLineage]:
        """Get lineage for asset."""
        return self._lineage.get(key)
    
    def add_dependency(self, upstream_key: str, downstream_key: str) -> None:
        """Add dependency between assets."""
        # Update upstream asset
        if upstream_key in self._lineage:
            self._lineage[upstream_key].add_downstream(downstream_key)
        
        # Update downstream asset
        if downstream_key in self._lineage:
            self._lineage[downstream_key].add_upstream(upstream_key)
        
        # Update consuming states
        upstream = self._assets.get(upstream_key)
        downstream = self._assets.get(downstream_key)
        
        if upstream and downstream and downstream.producing_state:
            upstream.consuming_states.add(downstream.producing_state)
    
    def get_state_assets(self, state_name: str) -> Set[str]:
        """Get assets produced by a state."""
        return self._state_assets.get(state_name, set())
    
    def get_stale_assets(self) -> List[Asset]:
        """Get all stale assets."""
        stale = []
        
        for asset in self._assets.values():
            lineage = self._lineage.get(asset.key)
            if lineage:
                upstream_assets = [
                    self._assets[key] for key in lineage.upstream_assets
                    if key in self._assets
                ]
                
                if asset.is_stale(upstream_assets):
                    stale.append(asset)
        
        return stale
    
    def materialize_asset(
        self,
        key: str,
        metadata: Optional[AssetMetadata] = None,
        version: Optional[str] = None
    ) -> None:
        """Mark asset as materialized."""
        asset = self._assets.get(key)
        if asset:
            asset.materialization_status = AssetMaterialization.COMPLETED
            asset.last_materialized = datetime.utcnow()
            
            if metadata:
                asset.metadata = metadata
            
            if version:
                asset.version = version
            
            logger.info(
                "asset_materialized",
                asset_key=key,
                version=version
            )
    
    def invalidate_downstream(self, key: str) -> List[str]:
        """Invalidate all downstream assets."""
        invalidated = []
        lineage = self._lineage.get(key)
        
        if lineage:
            full_lineage = lineage.get_full_lineage(self)
            
            for downstream_key in full_lineage["downstream"]:
                asset = self._assets.get(downstream_key)
                if asset:
                    asset.materialization_status = AssetMaterialization.STALE
                    invalidated.append(downstream_key)
        
        return invalidated
    
    def get_materialization_plan(
        self,
        target_assets: List[str]
    ) -> List[str]:
        """Get ordered list of assets to materialize."""
        # Build dependency graph
        from core.dag.graph import DAG, DAGNode, DAGEdge
        
        dag = DAG("materialization_plan")
        
        # Add all required assets
        required = set(target_assets)
        queue = list(target_assets)
        
        while queue:
            asset_key = queue.pop(0)
            lineage = self._lineage.get(asset_key)
            
            if lineage:
                for upstream in lineage.upstream_assets:
                    if upstream not in required:
                        required.add(upstream)
                        queue.append(upstream)
        
        # Build DAG
        for asset_key in required:
            asset = self._assets.get(asset_key)
            if asset:
                node = DAGNode(id=asset_key, data=asset)
                dag.add_node(node)
        
        # Add edges
        for asset_key in required:
            lineage = self._lineage.get(asset_key)
            if lineage:
                for upstream in lineage.upstream_assets:
                    if upstream in required:
                        edge = DAGEdge(source=upstream, target=asset_key)
                        dag.add_edge(edge)
        
        # Get topological order
        try:
            dag.validate()
            return dag.topological_sort()
        except Exception as e:
            logger.error("materialization_plan_error", error=str(e))
            return []


class PartitionManager:
    """Manages partitioned assets."""
    
    def __init__(self):
        self._partitions: Dict[str, Dict[str, Asset]] = {}  # asset_key -> partition -> asset
    
    def add_partition(
        self,
        asset_key: str,
        partition: str,
        asset: Asset
    ) -> None:
        """Add a partition for an asset."""
        if asset_key not in self._partitions:
            self._partitions[asset_key] = {}
        
        self._partitions[asset_key][partition] = asset
    
    def get_partition(
        self,
        asset_key: str,
        partition: str
    ) -> Optional[Asset]:
        """Get specific partition of an asset."""
        if asset_key in self._partitions:
            return self._partitions[asset_key].get(partition)
        return None
    
    def list_partitions(self, asset_key: str) -> List[str]:
        """List all partitions for an asset."""
        if asset_key in self._partitions:
            return list(self._partitions[asset_key].keys())
        return []
    
    def get_missing_partitions(
        self,
        asset_key: str,
        expected_partitions: List[str]
    ) -> List[str]:
        """Get partitions that haven't been materialized."""
        existing = set(self.list_partitions(asset_key))
        expected = set(expected_partitions)
        return list(expected - existing)
    
    def get_latest_partition(self, asset_key: str) -> Optional[str]:
        """Get the most recently materialized partition."""
        partitions = self._partitions.get(asset_key, {})
        
        latest_partition = None
        latest_time = None
        
        for partition, asset in partitions.items():
            if asset.last_materialized:
                if latest_time is None or asset.last_materialized > latest_time:
                    latest_time = asset.last_materialized
                    latest_partition = partition
        
        return latest_partition


# Asset decorator for states
def asset(
    key: str,
    description: str = "",
    path: Optional[str] = None,
    partitions: Optional[List[str]] = None,
    metadata: Optional[Dict[str, Any]] = None
):
    """Decorator to mark a state as producing an asset."""
    def decorator(func):
        # Store asset metadata on function
        func._asset_config = {
            "key": key,
            "description": description,
            "path": path,
            "partitions": partitions,
            "metadata": metadata
        }
        
        async def wrapper(context):
            # Register asset before execution
            catalog = context.get_state("_asset_catalog")
            if catalog:
                asset_obj = Asset(
                    key=key,
                    description=description,
                    path=path,
                    producing_state=func.__name__,
                    partitions=partitions,
                    metadata=AssetMetadata(**(metadata or {}))
                )
                catalog.register_asset(asset_obj)
            
            # Execute function
            result = await func(context)
            
            # Mark as materialized
            if catalog:
                catalog.materialize_asset(key)
            
            return result
        
        wrapper._asset_config = func._asset_config
        return wrapper
    
    return decorator