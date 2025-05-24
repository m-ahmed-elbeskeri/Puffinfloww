"""OpenAI plugin states implementation."""

import json
from typing import Dict, Any, List, Optional
import openai
from openai import AsyncOpenAI

from core.plugins.base import PluginState
from core.agent.context import Context
from core.agent.state import StateResult


class ChatCompletionState(PluginState):
    """Generate chat completions using OpenAI GPT models."""
    
    async def execute(self, context: Context) -> StateResult:
        """Execute chat completion."""
        # Get configuration
        model = self.config.get("model", "gpt-4")
        messages = self.config["messages"]
        temperature = self.config.get("temperature", 0.7)
        max_tokens = self.config.get("max_tokens", 1000)
        api_key = self.config.get("api_key") or context.get_secret("openai_api_key")
        
        if not api_key:
            raise ValueError("OpenAI API key is required")
        
        # Create client
        client = AsyncOpenAI(api_key=api_key)
        
        try:
            # Make API call
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens
            )
            
            # Extract response
            content = response.choices[0].message.content
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens
            }
            
            # Store outputs
            context.set_output("response", content)
            context.set_output("usage", usage)
            
            # Store in context for chaining
            context.set_state("last_ai_response", content)
            
        except Exception as e:
            raise RuntimeError(f"OpenAI API error: {str(e)}")
        
        return None
    
    def validate_inputs(self, context: Context) -> None:
        """Validate required inputs."""
        if not self.config.get("messages"):
            raise ValueError("Messages are required")
        
        # Validate message format
        for msg in self.config["messages"]:
            if not isinstance(msg, dict):
                raise ValueError("Each message must be a dictionary")
            if "role" not in msg or "content" not in msg:
                raise ValueError("Each message must have 'role' and 'content'")
            if msg["role"] not in ["system", "user", "assistant"]:
                raise ValueError(f"Invalid role: {msg['role']}")


class EmbeddingsState(PluginState):
    """Generate embeddings using OpenAI models."""
    
    async def execute(self, context: Context) -> StateResult:
        """Generate embeddings."""
        # Get configuration
        model = self.config.get("model", "text-embedding-ada-002")
        input_text = self.config["input"]
        api_key = self.config.get("api_key") or context.get_secret("openai_api_key")
        
        if not api_key:
            raise ValueError("OpenAI API key is required")
        
        # Create client
        client = AsyncOpenAI(api_key=api_key)
        
        try:
            # Generate embedding
            response = await client.embeddings.create(
                model=model,
                input=input_text
            )
            
            # Extract embedding
            embedding = response.data[0].embedding
            
            # Store outputs
            context.set_output("embedding", embedding)
            context.set_output("dimensions", len(embedding))
            
            # Store in context for use by other states
            context.set_state("last_embedding", embedding)
            
        except Exception as e:
            raise RuntimeError(f"OpenAI API error: {str(e)}")
        
        return None
    
    def validate_inputs(self, context: Context) -> None:
        """Validate required inputs."""
        if not self.config.get("input"):
            raise ValueError("Input text is required")


class FunctionCallingState(PluginState):
    """Use OpenAI function calling."""
    
    async def execute(self, context: Context) -> StateResult:
        """Execute function calling."""
        # Get configuration
        model = self.config.get("model", "gpt-4")
        messages = self.config["messages"]
        functions = self.config["functions"]
        api_key = self.config.get("api_key") or context.get_secret("openai_api_key")
        
        if not api_key:
            raise ValueError("OpenAI API key is required")
        
        # Create client
        client = AsyncOpenAI(api_key=api_key)
        
        try:
            # Make API call with functions
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                functions=functions,
                function_call="auto"
            )
            
            message = response.choices[0].message
            
            # Check if function was called
            if message.function_call:
                function_call = {
                    "name": message.function_call.name,
                    "arguments": json.loads(message.function_call.arguments)
                }
                context.set_output("function_call", function_call)
                
                # Store for potential execution
                context.set_state("pending_function_call", function_call)
            
            # Store response
            context.set_output("response", message.content)
            
        except Exception as e:
            raise RuntimeError(f"OpenAI API error: {str(e)}")
        
        return None
    
    def validate_inputs(self, context: Context) -> None:
        """Validate required inputs."""
        if not self.config.get("messages"):
            raise ValueError("Messages are required")
        
        if not self.config.get("functions"):
            raise ValueError("Functions are required")
        
        # Validate function format
        for func in self.config["functions"]:
            if not isinstance(func, dict):
                raise ValueError("Each function must be a dictionary")
            if "name" not in func or "parameters" not in func:
                raise ValueError("Each function must have 'name' and 'parameters'")