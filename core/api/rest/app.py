"""FastAPI REST API application."""

from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from typing import Dict, Any, List, Optional
import structlog

from core.config import get_settings
from core.execution.engine import WorkflowEngine
from core.storage.backends.sqlite import SQLiteBackend
from core.agent.base import Agent
from core.api.rest.models import (
    WorkflowCreate, WorkflowResponse, WorkflowStatus,
    StateAdd, WorkflowPause, WorkflowResume
)


logger = structlog.get_logger(__name__)
settings = get_settings()

# Create FastAPI app
app = FastAPI(
    title="Workflow Orchestrator API",
    version="0.1.0",
    docs_url=f"{settings.api_prefix}/docs",
    redoc_url=f"{settings.api_prefix}/redoc"
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global instances
engine: Optional[WorkflowEngine] = None
agents: Dict[str, Agent] = {}


@app.on_event("startup")
async def startup_event():
    """Initialize the workflow engine on startup."""
    global engine
    
    # Initialize storage
    storage = SQLiteBackend(settings.database_url)
    
    # Create workflow engine
    engine = WorkflowEngine(storage)
    await engine.start()
    
    logger.info("api_started", host=settings.api_host, port=settings.api_port)


@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on shutdown."""
    global engine
    
    if engine:
        await engine.stop()
    
    logger.info("api_stopped")


@app.get(f"{settings.api_prefix}/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "version": "0.1.0",
        "environment": settings.environment
    }


@app.post(
    f"{settings.api_prefix}/workflows",
    response_model=WorkflowResponse
)
async def create_workflow(
    workflow: WorkflowCreate,
    background_tasks: BackgroundTasks
):
    """Create a new workflow."""
    # Create agent
    agent = Agent(
        name=workflow.agent_name,
        max_concurrent=workflow.max_concurrent or settings.worker_concurrency
    )
    
    # Store agent for later use
    workflow_id = await engine.create_workflow(
        name=workflow.name,
        agent=agent,
        metadata=workflow.metadata
    )
    
    agents[workflow_id] = agent
    
    # Start execution in background if requested
    if workflow.auto_start:
        background_tasks.add_task(
            engine.execute_workflow,
            workflow_id,
            agent,
            workflow.timeout
        )
    
    return WorkflowResponse(
        workflow_id=workflow_id,
        name=workflow.name,
        agent_name=workflow.agent_name,
        status="created" if not workflow.auto_start else "starting"
    )


@app.post(
    f"{settings.api_prefix}/workflows/{{workflow_id}}/states",
    response_model=Dict[str, str]
)
async def add_state(
    workflow_id: str,
    state: StateAdd
):
    """Add a state to a workflow."""
    if workflow_id not in agents:
        raise HTTPException(status_code=404, detail="Workflow not found")
    
    agent = agents[workflow_id]
    
    # Create state function
    async def state_function(context):
        # This is a placeholder - in real implementation, this would
        # execute the actual state logic based on state.type
        context.set_state("executed", state.name)
        return state.transitions
    
    # Add state to agent
    agent.add_state(
        name=state.name,
        func=state_function,
        dependencies=state.dependencies,
        resources=state.resources,
        max_retries=state.max_retries
    )
    
    return {"status": "added", "state": state.name}


@app.post(
    f"{settings.api_prefix}/workflows/{{workflow_id}}/execute",
    response_model=Dict[str, str]
)
async def execute_workflow(
    workflow_id: str,
    background_tasks: BackgroundTasks,
    timeout: Optional[float] = None
):
    """Execute a workflow."""
    if workflow_id not in agents:
        raise HTTPException(status_code=404, detail="Workflow not found")
    
    agent = agents[workflow_id]
    
    background_tasks.add_task(
        engine.execute_workflow,
        workflow_id,
        agent,
        timeout
    )
    
    return {"status": "executing", "workflow_id": workflow_id}


@app.get(
    f"{settings.api_prefix}/workflows/{{workflow_id}}/status",
    response_model=WorkflowStatus
)
async def get_workflow_status(workflow_id: str):
    """Get workflow status."""
    try:
        status = await engine.get_workflow_status(workflow_id)
        return WorkflowStatus(**status)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post(
    f"{settings.api_prefix}/workflows/{{workflow_id}}/pause",
    response_model=Dict[str, Any]
)
async def pause_workflow(workflow_id: str):
    """Pause a running workflow."""
    try:
        result = await engine.pause_workflow(workflow_id)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post(
    f"{settings.api_prefix}/workflows/{{workflow_id}}/resume",
    response_model=Dict[str, str]
)
async def resume_workflow(
    workflow_id: str,
    background_tasks: BackgroundTasks
):
    """Resume a paused workflow."""
    if workflow_id not in agents:
        raise HTTPException(status_code=404, detail="Workflow not found")
    
    agent = agents[workflow_id]
    
    background_tasks.add_task(
        engine.resume_workflow,
        workflow_id,
        agent
    )
    
    return {"status": "resuming", "workflow_id": workflow_id}


@app.delete(
    f"{settings.api_prefix}/workflows/{{workflow_id}}",
    response_model=Dict[str, str]
)
async def cancel_workflow(workflow_id: str):
    """Cancel a workflow."""
    await engine.cancel_workflow(workflow_id)
    
    # Clean up agent
    agents.pop(workflow_id, None)
    
    return {"status": "cancelled", "workflow_id": workflow_id}


@app.get(
    f"{settings.api_prefix}/workflows",
    response_model=List[Dict[str, Any]]
)
async def list_workflows(
    limit: int = 100,
    offset: int = 0
):
    """List all workflows."""
    workflows = await engine.storage.list_workflows(limit=limit, offset=offset)
    return workflows