"""Webhook plugin states implementation."""

import asyncio
import json
import hmac
import hashlib
from typing import Dict, Any, Optional
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from core.plugins.base import PluginState
from core.agent.context import Context
from core.agent.state import StateResult


class HTTPRequestState(PluginState):
    """Make HTTP requests."""
    
    async def execute(self, context: Context) -> StateResult:
        """Execute HTTP request."""
        url = self.config["url"]
        method = self.config.get("method", "GET")
        headers = self.config.get("headers", {})
        body = self.config.get("body")
        timeout = self.config.get("timeout", 30)
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    json=body if isinstance(body, dict) else None,
                    data=body if isinstance(body, str) else None,
                    timeout=timeout
                )
                
                # Store results
                context.set_output("status_code", response.status_code)
                context.set_output("headers", dict(response.headers))
                
                # Parse response body
                try:
                    response_body = response.json()
                except:
                    response_body = response.text
                
                context.set_output("body", response_body)
                
                # Check for success
                response.raise_for_status()
                
        except httpx.HTTPStatusError as e:
            context.set_output("error", f"HTTP {e.response.status_code}: {str(e)}")
            if self.config.get("fail_on_error", True):
                raise
                
        except Exception as e:
            context.set_output("error", str(e))
            if self.config.get("fail_on_error", True):
                raise
        
        return None
    
    def validate_inputs(self, context: Context) -> None:
        """Validate required inputs."""
        if not self.config.get("url"):
            raise ValueError("URL is required")
        
        method = self.config.get("method", "GET")
        if method not in ["GET", "POST", "PUT", "DELETE", "PATCH"]:
            raise ValueError(f"Invalid HTTP method: {method}")


class WebhookReceiverState(PluginState):
    """Receive webhook callbacks."""
    
    # Class-level storage for webhook data
    _webhook_data: Dict[str, asyncio.Queue] = {}
    
    async def execute(self, context: Context) -> StateResult:
        """Wait for webhook callback."""
        endpoint = self.config["endpoint"]
        timeout = self.config.get("timeout", 300)
        validation = self.config.get("validation", {})
        
        # Create queue for this endpoint if not exists
        if endpoint not in self._webhook_data:
            self._webhook_data[endpoint] = asyncio.Queue()
        
        queue = self._webhook_data[endpoint]
        
        try:
            # Wait for webhook data
            webhook_data = await asyncio.wait_for(
                queue.get(),
                timeout=timeout
            )
            
            # Validate webhook if configured
            if validation.get("secret") and validation.get("signature_header"):
                signature = webhook_data["headers"].get(
                    validation["signature_header"]
                )
                if not self._validate_signature(
                    webhook_data["payload"],
                    signature,
                    validation["secret"]
                ):
                    raise ValueError("Invalid webhook signature")
            
            # Store webhook data
            context.set_output("payload", webhook_data["payload"])
            context.set_output("headers", webhook_data["headers"])
            
        except asyncio.TimeoutError:
            raise TimeoutError(f"No webhook received within {timeout} seconds")
        
        return None
    
    def _validate_signature(
        self,
        payload: Any,
        signature: str,
        secret: str
    ) -> bool:
        """Validate webhook signature."""
        if not signature:
            return False
        
        # Compute expected signature
        payload_bytes = json.dumps(payload).encode() if isinstance(payload, dict) else str(payload).encode()
        expected = hmac.new(
            secret.encode(),
            payload_bytes,
            hashlib.sha256
        ).hexdigest()
        
        # Compare signatures
        return hmac.compare_digest(signature, expected)
    
    @classmethod
    async def handle_webhook(
        cls,
        endpoint: str,
        payload: Any,
        headers: Dict[str, str]
    ) -> None:
        """Handle incoming webhook (called by API endpoint)."""
        if endpoint in cls._webhook_data:
            await cls._webhook_data[endpoint].put({
                "payload": payload,
                "headers": headers
            })


class WebhookSenderState(PluginState):
    """Send webhook notifications."""
    
    async def execute(self, context: Context) -> StateResult:
        """Send webhook notification."""
        url = self.config["url"]
        payload = self.config["payload"]
        secret = self.config.get("secret")
        retry_config = self.config.get("retry", {})
        
        # Prepare headers
        headers = {"Content-Type": "application/json"}
        
        # Add signature if secret provided
        if secret:
            payload_bytes = json.dumps(payload).encode()
            signature = hmac.new(
                secret.encode(),
                payload_bytes,
                hashlib.sha256
            ).hexdigest()
            headers["X-Webhook-Signature"] = signature
        
        # Configure retry
        max_attempts = retry_config.get("max_attempts", 3)
        backoff = retry_config.get("backoff", 1.0)
        
        @retry(
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential(multiplier=backoff)
        )
        async def send_webhook():
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=30
                )
                response.raise_for_status()
                return response
        
        try:
            response = await send_webhook()
            context.set_output("success", True)
            context.set_output("response", {
                "status_code": response.status_code,
                "body": response.text
            })
            
        except Exception as e:
            context.set_output("success", False)
            context.set_output("response", {"error": str(e)})
            
            if self.config.get("fail_on_error", True):
                raise
        
        return None
    
    def validate_inputs(self, context: Context) -> None:
        """Validate required inputs."""
        if not self.config.get("url"):
            raise ValueError("URL is required")
        
        if "payload" not in self.config:
            raise ValueError("Payload is required")