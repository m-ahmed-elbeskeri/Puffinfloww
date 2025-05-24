"""Integration system for connecting to external services."""

from typing import Dict, List, Optional, Any, Type
from dataclasses import dataclass, field
from abc import ABC, abstractmethod
import importlib
import pkgutil
from pathlib import Path
import structlog

from core.workflow.node import Node, NodeDefinition, NodeParameter, ParameterType, NodeData

logger = structlog.get_logger(__name__)


@dataclass
class IntegrationCredentials:
    """Credentials for an integration."""
    name: str
    type: str  # oauth2, apiKey, basicAuth, etc.
    properties: Dict[str, Any] = field(default_factory=dict)
    
    def get(self, key: str, default: Any = None) -> Any:
        """Get credential value."""
        return self.properties.get(key, default)


class Integration(ABC):
    """Base class for integrations."""
    
    def __init__(self):
        self.name = self.__class__.__name__.lower().replace("integration", "")
        self._nodes: Dict[str, Type[Node]] = {}
        self._credentials: Optional[IntegrationCredentials] = None
    
    @abstractmethod
    def get_nodes(self) -> List[Type[Node]]:
        """Get all nodes provided by this integration."""
        pass
    
    def set_credentials(self, credentials: IntegrationCredentials) -> None:
        """Set credentials for the integration."""
        self._credentials = credentials
    
    def get_api_client(self) -> Any:
        """Get configured API client for the integration."""
        return None


# Built-in integrations
class HTTPIntegration(Integration):
    """HTTP/REST API integration."""
    
    def get_nodes(self) -> List[Type[Node]]:
        return [HTTPRequestNode]


class HTTPRequestNode(Node):
    """Make HTTP requests."""
    
    @property
    def definition(self) -> NodeDefinition:
        return NodeDefinition(
            name="httpRequest",
            display_name="HTTP Request",
            description="Make HTTP requests",
            group=["network"],
            version=1,
            defaults={"name": "HTTP Request"},
            properties=[
                NodeParameter(
                    name="method",
                    display_name="Method",
                    type=ParameterType.OPTIONS,
                    default="GET",
                    options=[
                        {"name": "GET", "value": "GET"},
                        {"name": "POST", "value": "POST"},
                        {"name": "PUT", "value": "PUT"},
                        {"name": "PATCH", "value": "PATCH"},
                        {"name": "DELETE", "value": "DELETE"},
                        {"name": "HEAD", "value": "HEAD"},
                        {"name": "OPTIONS", "value": "OPTIONS"}
                    ]
                ),
                NodeParameter(
                    name="url",
                    display_name="URL",
                    type=ParameterType.STRING,
                    default="",
                    required=True,
                    placeholder="https://api.example.com/endpoint"
                ),
                NodeParameter(
                    name="authentication",
                    display_name="Authentication",
                    type=ParameterType.OPTIONS,
                    default="none",
                    options=[
                        {"name": "None", "value": "none"},
                        {"name": "Basic Auth", "value": "basicAuth"},
                        {"name": "Bearer Token", "value": "bearerToken"},
                        {"name": "API Key", "value": "apiKey"},
                        {"name": "OAuth2", "value": "oauth2"}
                    ]
                ),
                NodeParameter(
                    name="headers",
                    display_name="Headers",
                    type=ParameterType.COLLECTION,
                    default={},
                    placeholder="Add Header",
                    type_options={
                        "multipleValues": True
                    }
                ),
                NodeParameter(
                    name="queryParameters",
                    display_name="Query Parameters",
                    type=ParameterType.COLLECTION,
                    default={},
                    placeholder="Add Parameter",
                    type_options={
                        "multipleValues": True
                    }
                ),
                NodeParameter(
                    name="body",
                    display_name="Body",
                    type=ParameterType.JSON,
                    default="",
                    display_options={
                        "show": {
                            "method": ["POST", "PUT", "PATCH"]
                        }
                    }
                ),
                NodeParameter(
                    name="responseFormat",
                    display_name="Response Format",
                    type=ParameterType.OPTIONS,
                    default="json",
                    options=[
                        {"name": "JSON", "value": "json"},
                        {"name": "Text", "value": "text"},
                        {"name": "Binary", "value": "binary"}
                    ]
                )
            ],
            icon="fa:globe",
            icon_color="#2200CC"
        )
    
    async def execute(
        self,
        input_data: List[List[NodeData]],
        context: Dict[str, Any]
    ) -> List[List[NodeData]]:
        """Execute HTTP request."""
        import httpx
        
        method = self.get_parameter("method", "GET")
        url = self.get_parameter("url")
        headers = self.get_parameter("headers", {})
        query_params = self.get_parameter("queryParameters", {})
        body = self.get_parameter("body")
        response_format = self.get_parameter("responseFormat", "json")
        
        output_data = []
        items = input_data[0] if input_data else [NodeData()]
        
        async with httpx.AsyncClient() as client:
            for item in items:
                # Resolve expressions in parameters
                resolved_url = self._resolve_expressions(url, item)
                resolved_headers = self._resolve_dict_expressions(headers, item)
                resolved_params = self._resolve_dict_expressions(query_params, item)
                
                # Prepare request
                request_kwargs = {
                    "method": method,
                    "url": resolved_url,
                    "headers": resolved_headers,
                    "params": resolved_params
                }
                
                if method in ["POST", "PUT", "PATCH"] and body:
                    if isinstance(body, str):
                        request_kwargs["json"] = json.loads(body)
                    else:
                        request_kwargs["json"] = body
                
                # Make request
                response = await client.request(**request_kwargs)
                
                # Process response
                result = {
                    "statusCode": response.status_code,
                    "headers": dict(response.headers)
                }
                
                if response_format == "json":
                    try:
                        result["body"] = response.json()
                    except:
                        result["body"] = response.text
                elif response_format == "text":
                    result["body"] = response.text
                else:  # binary
                    result["body"] = response.content
                
                output_data.append(NodeData(json=result))
        
        return self.prepare_output_data(output_data)
    
    def _resolve_expressions(self, value: str, item: NodeData) -> str:
        """Resolve expressions in string."""
        if not isinstance(value, str):
            return value
        
        # Simple expression resolution
        if "{{" in value:
            import re
            pattern = r'\{\{([^}]+)\}\}'
            
            def replacer(match):
                expr = match.group(1).strip()
                if expr.startswith("$json."):
                    return str(item.get(expr[6:], ""))
                return match.group(0)
            
            return re.sub(pattern, replacer, value)
        
        return value
    
    def _resolve_dict_expressions(self, data: Dict[str, Any], item: NodeData) -> Dict[str, Any]:
        """Resolve expressions in dictionary."""
        resolved = {}
        for key, value in data.items():
            resolved[key] = self._resolve_expressions(value, item)
        return resolved


class DatabaseIntegration(Integration):
    """Database integration."""
    
    def get_nodes(self) -> List[Type[Node]]:
        return [DatabaseQueryNode, DatabaseInsertNode]


class DatabaseQueryNode(Node):
    """Query database."""
    
    @property
    def definition(self) -> NodeDefinition:
        return NodeDefinition(
            name="databaseQuery",
            display_name="Database Query",
            description="Query a database",
            group=["database"],
            version=1,
            defaults={"name": "Database Query"},
            properties=[
                NodeParameter(
                    name="database",
                    display_name="Database",
                    type=ParameterType.OPTIONS,
                    default="postgres",
                    options=[
                        {"name": "PostgreSQL", "value": "postgres"},
                        {"name": "MySQL", "value": "mysql"},
                        {"name": "SQLite", "value": "sqlite"},
                        {"name": "MongoDB", "value": "mongodb"}
                    ]
                ),
                NodeParameter(
                    name="connectionString",
                    display_name="Connection String",
                    type=ParameterType.STRING,
                    default="",
                    required=True,
                    placeholder="postgresql://user:pass@host:5432/db"
                ),
                NodeParameter(
                    name="query",
                    display_name="Query",
                    type=ParameterType.CODE,
                    default="SELECT * FROM table",
                    type_options={
                        "editor": "sql"
                    }
                ),
                NodeParameter(
                    name="parameters",
                    display_name="Query Parameters",
                    type=ParameterType.COLLECTION,
                    default={},
                    description="Parameters to bind to the query"
                )
            ],
            icon="fa:database",
            icon_color="#00AA44"
        )
    
    async def execute(
        self,
        input_data: List[List[NodeData]],
        context: Dict[str, Any]
    ) -> List[List[NodeData]]:
        """Execute database query."""
        # Simplified implementation
        database = self.get_parameter("database")
        query = self.get_parameter("query")
        
        # Mock result
        results = [
            {"id": 1, "name": "Item 1", "value": 100},
            {"id": 2, "name": "Item 2", "value": 200}
        ]
        
        output_data = [NodeData(json=row) for row in results]
        return self.prepare_output_data(output_data)


class SlackIntegration(Integration):
    """Slack integration."""
    
    def get_nodes(self) -> List[Type[Node]]:
        return [SlackMessageNode, SlackChannelNode]


class SlackMessageNode(Node):
    """Send Slack message."""
    
    @property
    def definition(self) -> NodeDefinition:
        return NodeDefinition(
            name="slack",
            display_name="Slack",
            description="Send messages to Slack",
            group=["communication"],
            version=1,
            defaults={"name": "Slack"},
            credentials=["slackApi"],
            properties=[
                NodeParameter(
                    name="resource",
                    display_name="Resource",
                    type=ParameterType.OPTIONS,
                    default="message",
                    options=[
                        {"name": "Message", "value": "message"},
                        {"name": "Channel", "value": "channel"},
                        {"name": "User", "value": "user"}
                    ]
                ),
                NodeParameter(
                    name="operation",
                    display_name="Operation",
                    type=ParameterType.OPTIONS,
                    default="send",
                    options=[
                        {"name": "Send", "value": "send"},
                        {"name": "Update", "value": "update"},
                        {"name": "Delete", "value": "delete"}
                    ],
                    display_options={
                        "show": {
                            "resource": ["message"]
                        }
                    }
                ),
                NodeParameter(
                    name="channel",
                    display_name="Channel",
                    type=ParameterType.STRING,
                    default="",
                    required=True,
                    placeholder="#general or @user",
                    display_options={
                        "show": {
                            "resource": ["message"],
                            "operation": ["send"]
                        }
                    }
                ),
                NodeParameter(
                    name="text",
                    display_name="Text",
                    type=ParameterType.STRING,
                    default="",
                    required=True,
                    type_options={
                        "rows": 5
                    },
                    display_options={
                        "show": {
                            "resource": ["message"],
                            "operation": ["send"]
                        }
                    }
                ),
                NodeParameter(
                    name="attachments",
                    display_name="Attachments",
                    type=ParameterType.COLLECTION,
                    default=[],
                    display_options={
                        "show": {
                            "resource": ["message"],
                            "operation": ["send"]
                        }
                    }
                )
            ],
            icon="fab:slack",
            icon_color="#4A1850"
        )
    
    async def execute(
        self,
        input_data: List[List[NodeData]],
        context: Dict[str, Any]
    ) -> List[List[NodeData]]:
        """Send Slack message."""
        resource = self.get_parameter("resource")
        operation = self.get_parameter("operation")
        
        if resource == "message" and operation == "send":
            channel = self.get_parameter("channel")
            text = self.get_parameter("text")
            
            # Mock sending message
            result = {
                "ok": True,
                "channel": channel,
                "ts": "1234567890.123456",
                "message": {
                    "text": text,
                    "user": "bot",
                    "ts": "1234567890.123456"
                }
            }
            
            return self.prepare_output_data(result)
        
        return self.prepare_output_data({})


class EmailIntegration(Integration):
    """Email integration."""
    
    def get_nodes(self) -> List[Type[Node]]:
        return [EmailSendNode, EmailTriggerNode]


class EmailSendNode(Node):
    """Send email."""
    
    @property
    def definition(self) -> NodeDefinition:
        return NodeDefinition(
            name="emailSend",
            display_name="Send Email",
            description="Send an email",
            group=["communication"],
            version=1,
            defaults={"name": "Send Email"},
            credentials=["smtp"],
            properties=[
                NodeParameter(
                    name="fromEmail",
                    display_name="From Email",
                    type=ParameterType.STRING,
                    default="",
                    placeholder="sender@example.com"
                ),
                NodeParameter(
                    name="toEmail",
                    display_name="To Email",
                    type=ParameterType.STRING,
                    default="",
                    required=True,
                    placeholder="recipient@example.com"
                ),
                NodeParameter(
                    name="subject",
                    display_name="Subject",
                    type=ParameterType.STRING,
                    default="",
                    required=True
                ),
                NodeParameter(
                    name="text",
                    display_name="Text",
                    type=ParameterType.STRING,
                    default="",
                    type_options={
                        "rows": 5
                    }
                ),
                NodeParameter(
                    name="html",
                    display_name="HTML",
                    type=ParameterType.STRING,
                    default="",
                    type_options={
                        "rows": 10
                    }
                ),
                NodeParameter(
                    name="attachments",
                    display_name="Attachments",
                    type=ParameterType.STRING,
                    default="",
                    description="Comma-separated list of attachment URLs"
                )
            ],
            icon="fa:envelope",
            icon_color="#FF6600"
        )
    
    async def execute(
        self,
        input_data: List[List[NodeData]],
        context: Dict[str, Any]
    ) -> List[List[NodeData]]:
        """Send email."""
        to_email = self.get_parameter("toEmail")
        subject = self.get_parameter("subject")
        text = self.get_parameter("text")
        html = self.get_parameter("html")
        
        # Mock sending email
        result = {
            "success": True,
            "messageId": f"<{uuid.uuid4()}@example.com>",
            "to": to_email,
            "subject": subject
        }
        
        return self.prepare_output_data(result)


class GoogleSheetsIntegration(Integration):
    """Google Sheets integration."""
    
    def get_nodes(self) -> List[Type[Node]]:
        return [GoogleSheetsReadNode, GoogleSheetsWriteNode]


class TransformIntegration(Integration):
    """Data transformation integration."""
    
    def get_nodes(self) -> List[Type[Node]]:
        return [
            MergeNode,
            SplitNode,
            FilterNode,
            SortNode,
            LimitNode,
            AggregateNode,
            CodeNode
        ]


class CodeNode(Node):
    """Execute custom code."""
    
    @property
    def definition(self) -> NodeDefinition:
        return NodeDefinition(
            name="code",
            display_name="Code",
            description="Execute custom JavaScript code",
            group=["transform"],
            version=1,
            defaults={"name": "Code"},
            properties=[
                NodeParameter(
                    name="language",
                    display_name="Language",
                    type=ParameterType.OPTIONS,
                    default="javascript",
                    options=[
                        {"name": "JavaScript", "value": "javascript"},
                        {"name": "Python", "value": "python"}
                    ]
                ),
                NodeParameter(
                    name="code",
                    display_name="Code",
                    type=ParameterType.CODE,
                    default="""// Available variables:
// items - Input items
// $input - Input data
// $json - Current item data

for (const item of items) {
  item.json.newField = 'value';
}

return items;""",
                    type_options={
                        "editor": "javascript"
                    }
                )
            ],
            icon="fa:code",
            icon_color="#FF0000"
        )
    
    async def execute(
        self,
        input_data: List[List[NodeData]],
        context: Dict[str, Any]
    ) -> List[List[NodeData]]:
        """Execute custom code."""
        code = self.get_parameter("code")
        language = self.get_parameter("language", "javascript")
        
        items = input_data[0] if input_data else []
        
        # In real implementation, would execute code safely
        # For now, just pass through
        return self.prepare_output_data(items)


class IntegrationRegistry:
    """Registry for all integrations."""
    
    def __init__(self):
        self._integrations: Dict[str, Integration] = {}
        self._nodes: Dict[str, Type[Node]] = {}
        self._load_builtin_integrations()
    
    def _load_builtin_integrations(self):
        """Load built-in integrations."""
        builtin = [
            HTTPIntegration(),
            DatabaseIntegration(),
            SlackIntegration(),
            EmailIntegration(),
            GoogleSheetsIntegration(),
            TransformIntegration()
        ]
        
        for integration in builtin:
            self.register_integration(integration)
    
    def register_integration(self, integration: Integration) -> None:
        """Register an integration."""
        self._integrations[integration.name] = integration
        
        # Register nodes
        for node_class in integration.get_nodes():
            node_instance = node_class("temp")
            node_name = node_instance.definition.name
            self._nodes[node_name] = node_class
            
        logger.info(
            "integration_registered",
            name=integration.name,
            nodes=len(integration.get_nodes())
        )
    
    def get_integration(self, name: str) -> Optional[Integration]:
        """Get integration by name."""
        return self._integrations.get(name)
    
    def get_node_class(self, node_type: str) -> Optional[Type[Node]]:
        """Get node class by type."""
        return self._nodes.get(node_type)
    
    def list_integrations(self) -> List[str]:
        """List all registered integrations."""
        return list(self._integrations.keys())
    
    def list_nodes(self) -> List[Dict[str, Any]]:
        """List all available nodes."""
        nodes = []
        
        for node_name, node_class in self._nodes.items():
            node_instance = node_class("temp")
            definition = node_instance.definition
            
            nodes.append({
                "name": definition.name,
                "displayName": definition.display_name,
                "group": definition.group,
                "description": definition.description,
                "icon": definition.icon,
                "iconColor": definition.icon_color
            })
        
        return nodes
    
    def get_node_definition(self, node_type: str) -> Optional[NodeDefinition]:
        """Get node definition."""
        node_class = self.get_node_class(node_type)
        if node_class:
            node_instance = node_class("temp")
            return node_instance.definition
        return None


class IntegrationLoader:
    """Dynamic integration loader."""
    
    def __init__(self, registry: IntegrationRegistry):
        self.registry = registry
    
    def load_from_directory(self, directory: Path) -> None:
        """Load integrations from directory."""
        for module_info in pkgutil.iter_modules([str(directory)]):
            if module_info.ispkg:
                try:
                    module = importlib.import_module(f"{directory.name}.{module_info.name}")
                    
                    # Find Integration subclasses
                    for name, obj in module.__dict__.items():
                        if (isinstance(obj, type) and 
                            issubclass(obj, Integration) and 
                            obj != Integration):
                            
                            integration = obj()
                            self.registry.register_integration(integration)
                            
                except Exception as e:
                    logger.error(
                        "integration_load_error",
                        module=module_info.name,
                        error=str(e)
                    )


# Singleton registry
_registry = IntegrationRegistry()

def get_registry() -> IntegrationRegistry:
    """Get the global integration registry."""
    return _registry


import uuid
import json