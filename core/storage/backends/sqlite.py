"""SQLite storage backend implementation."""

import json
import pickle
from typing import List, Optional, Dict, Any
from datetime import datetime
import aiosqlite
from pathlib import Path

from core.storage.interface import StorageBackend
from core.storage.events import WorkflowEvent, EventType
from core.agent.checkpoint import AgentCheckpoint


class SQLiteBackend(StorageBackend):
    """SQLite implementation of storage backend."""
    
    def __init__(self, database_url: str):
        self.database_url = database_url
        self.db_path = database_url.replace("sqlite+aiosqlite:///", "")
        self._connection: Optional[aiosqlite.Connection] = None
    
    async def initialize(self) -> None:
        """Initialize the storage backend."""
        # Ensure directory exists
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        
        self._connection = await aiosqlite.connect(self.db_path)
        
        # Create tables
        await self._connection.executescript("""
            CREATE TABLE IF NOT EXISTS workflow_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                workflow_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                state_name TEXT,
                timestamp DATETIME NOT NULL,
                data TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            
            CREATE INDEX IF NOT EXISTS idx_workflow_events_workflow_id 
                ON workflow_events(workflow_id);
            CREATE INDEX IF NOT EXISTS idx_workflow_events_timestamp 
                ON workflow_events(timestamp);
            
            CREATE TABLE IF NOT EXISTS workflow_checkpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow_id TEXT NOT NULL,
                checkpoint_data BLOB NOT NULL,
                timestamp DATETIME NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            
            CREATE INDEX IF NOT EXISTS idx_workflow_checkpoints_workflow_id 
                ON workflow_checkpoints(workflow_id);
            
            CREATE TABLE IF NOT EXISTS workflows (
                workflow_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                agent_name TEXT NOT NULL,
                status TEXT NOT NULL,
                metadata TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        await self._connection.commit()
    
    async def close(self) -> None:
        """Close the storage backend."""
        if self._connection:
            await self._connection.close()
            self._connection = None
    
    async def save_event(self, event: WorkflowEvent) -> None:
        """Save a workflow event."""
        if not self._connection:
            raise RuntimeError("Storage backend not initialized")
        
        await self._connection.execute(
            """
            INSERT INTO workflow_events 
                (event_id, workflow_id, event_type, state_name, timestamp, data)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.workflow_id,
                event.event_type.value,
                event.state_name,
                event.timestamp.isoformat(),
                json.dumps(event.data)
            )
        )
        
        # Update workflow status if needed
        if event.event_type == EventType.WORKFLOW_CREATED:
            await self._connection.execute(
                """
                INSERT INTO workflows (workflow_id, name, agent_name, status, metadata)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event.workflow_id,
                    event.data.get("name", ""),
                    event.data.get("agent_name", ""),
                    "created",
                    json.dumps(event.data.get("metadata", {}))
                )
            )
        else:
            status_map = {
                EventType.WORKFLOW_STARTED: "running",
                EventType.WORKFLOW_COMPLETED: "completed",
                EventType.WORKFLOW_FAILED: "failed",
                EventType.WORKFLOW_CANCELLED: "cancelled",
                EventType.WORKFLOW_PAUSED: "paused",
                EventType.WORKFLOW_RESUMED: "running"
            }
            
            if event.event_type in status_map:
                await self._connection.execute(
                    """
                    UPDATE workflows 
                    SET status = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE workflow_id = ?
                    """,
                    (status_map[event.event_type], event.workflow_id)
                )
        
        await self._connection.commit()
    
    async def load_events(
        self,
        workflow_id: str,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None
    ) -> List[WorkflowEvent]:
        """Load events for a workflow."""
        if not self._connection:
            raise RuntimeError("Storage backend not initialized")
        
        query = "SELECT * FROM workflow_events WHERE workflow_id = ?"
        params = [workflow_id]
        
        if start_time:
            query += " AND timestamp >= ?"
            params.append(start_time.isoformat())
        
        if end_time:
            query += " AND timestamp <= ?"
            params.append(end_time.isoformat())
        
        query += " ORDER BY timestamp ASC"
        
        cursor = await self._connection.execute(query, params)
        rows = await cursor.fetchall()
        
        events = []
        for row in rows:
            event = WorkflowEvent(
                workflow_id=row[2],
                event_type=EventType(row[3]),
                timestamp=datetime.fromisoformat(row[5]),
                data=json.loads(row[6]),
                state_name=row[4],
                event_id=row[1]
            )
            events.append(event)
        
        return events
    
    async def save_checkpoint(
        self,
        workflow_id: str,
        checkpoint: AgentCheckpoint
    ) -> None:
        """Save an agent checkpoint."""
        if not self._connection:
            raise RuntimeError("Storage backend not initialized")
        
        checkpoint_data = pickle.dumps(checkpoint)
        
        await self._connection.execute(
            """
            INSERT INTO workflow_checkpoints (workflow_id, checkpoint_data, timestamp)
            VALUES (?, ?, ?)
            """,
            (workflow_id, checkpoint_data, datetime.utcnow().isoformat())
        )
        
        await self._connection.commit()
    
    async def load_checkpoint(
        self,
        workflow_id: str
    ) -> Optional[AgentCheckpoint]:
        """Load the latest checkpoint for a workflow."""
        if not self._connection:
            raise RuntimeError("Storage backend not initialized")
        
        cursor = await self._connection.execute(
            """
            SELECT checkpoint_data FROM workflow_checkpoints
            WHERE workflow_id = ?
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (workflow_id,)
        )
        
        row = await cursor.fetchone()
        if row:
            return pickle.loads(row[0])
        
        return None
    
    async def list_workflows(
        self,
        limit: int = 100,
        offset: int = 0,
        filters: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """List workflows with optional filtering."""
        if not self._connection:
            raise RuntimeError("Storage backend not initialized")
        
        query = "SELECT * FROM workflows"
        params = []
        
        if filters:
            conditions = []
            for key, value in filters.items():
                if key in ["name", "agent_name", "status"]:
                    conditions.append(f"{key} = ?")
                    params.append(value)
            
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
        
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        
        cursor = await self._connection.execute(query, params)
        rows = await cursor.fetchall()
        
        workflows = []
        for row in rows:
            workflows.append({
                "workflow_id": row[0],
                "name": row[1],
                "agent_name": row[2],
                "status": row[3],
                "metadata": json.loads(row[4]) if row[4] else {},
                "created_at": row[5],
                "updated_at": row[6]
            })
        
        return workflows
    
    async def delete_workflow(self, workflow_id: str) -> None:
        """Delete all data for a workflow."""
        if not self._connection:
            raise RuntimeError("Storage backend not initialized")
        
        await self._connection.execute(
            "DELETE FROM workflow_events WHERE workflow_id = ?",
            (workflow_id,)
        )
        
        await self._connection.execute(
            "DELETE FROM workflow_checkpoints WHERE workflow_id = ?",
            (workflow_id,)
        )
        
        await self._connection.execute(
            "DELETE FROM workflows WHERE workflow_id = ?",
            (workflow_id,)
        )
        
        await self._connection.commit()