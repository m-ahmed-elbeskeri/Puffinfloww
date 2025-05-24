"""Slack plugin states implementation."""

import base64
from typing import Dict, Any, List, Optional
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.errors import SlackApiError

from core.plugins.base import PluginState
from core.agent.context import Context
from core.agent.state import StateResult


class SlackBaseState(PluginState):
    """Base state for Slack operations."""
    
    def _get_client(self, token: str) -> AsyncWebClient:
        """Create Slack client instance."""
        return AsyncWebClient(token=token)


class SendMessageState(SlackBaseState):
    """Send a message to a Slack channel."""
    
    async def execute(self, context: Context) -> StateResult:
        """Send message to Slack."""
        # Get configuration
        channel = self.config["channel"]
        text = self.config["text"]
        thread_ts = self.config.get("thread_ts")
        blocks = self.config.get("blocks")
        attachments = self.config.get("attachments")
        token = self.config.get("token") or context.get_secret("slack_token")
        
        if not token:
            raise ValueError("Slack token is required")
        
        try:
            # Create client
            client = self._get_client(token)
            
            # Prepare message
            message_args = {
                "channel": channel,
                "text": text
            }
            
            if thread_ts:
                message_args["thread_ts"] = thread_ts
            if blocks:
                message_args["blocks"] = blocks
            if attachments:
                message_args["attachments"] = attachments
            
            # Send message
            response = await client.chat_postMessage(**message_args)
            
            # Store outputs
            context.set_output("ts", response["ts"])
            context.set_output("channel", response["channel"])
            if "thread_ts" in response:
                context.set_output("thread_ts", response["thread_ts"])
            
            # Store for threading
            context.set_state("last_message_ts", response["ts"])
            
        except SlackApiError as e:
            raise RuntimeError(f"Slack API error: {e.response['error']}")
        except Exception as e:
            raise RuntimeError(f"Failed to send message: {str(e)}")
        
        return None
    
    def validate_inputs(self, context: Context) -> None:
        """Validate required inputs."""
        if not self.config.get("channel"):
            raise ValueError("Channel is required")
        
        if not self.config.get("text") and not self.config.get("blocks"):
            raise ValueError("Either text or blocks is required")


class ReadMessagesState(SlackBaseState):
    """Read messages from a Slack channel."""
    
    async def execute(self, context: Context) -> StateResult:
        """Read messages from Slack."""
        # Get configuration
        channel = self.config["channel"]
        limit = self.config.get("limit", 10)
        oldest = self.config.get("oldest")
        latest = self.config.get("latest")
        token = self.config.get("token") or context.get_secret("slack_token")
        
        if not token:
            raise ValueError("Slack token is required")
        
        try:
            # Create client
            client = self._get_client(token)
            
            # Prepare request
            history_args = {
                "channel": channel,
                "limit": limit
            }
            
            if oldest:
                history_args["oldest"] = oldest
            if latest:
                history_args["latest"] = latest
            
            # Get messages
            response = await client.conversations_history(**history_args)
            
            # Parse messages
            messages = []
            for msg in response["messages"]:
                message_data = {
                    "ts": msg["ts"],
                    "user": msg.get("user"),
                    "text": msg.get("text", ""),
                    "thread_ts": msg.get("thread_ts"),
                    "reply_count": msg.get("reply_count", 0),
                    "reactions": msg.get("reactions", [])
                }
                
                # Include blocks if present
                if "blocks" in msg:
                    message_data["blocks"] = msg["blocks"]
                
                messages.append(message_data)
            
            # Store outputs
            context.set_output("messages", messages)
            context.set_output("has_more", response.get("has_more", False))
            
            # Store for processing
            context.set_state("slack_messages", messages)
            
        except SlackApiError as e:
            raise RuntimeError(f"Slack API error: {e.response['error']}")
        except Exception as e:
            raise RuntimeError(f"Failed to read messages: {str(e)}")
        
        return None
    
    def validate_inputs(self, context: Context) -> None:
        """Validate required inputs."""
        if not self.config.get("channel"):
            raise ValueError("Channel is required")


class UploadFileState(SlackBaseState):
    """Upload a file to Slack."""
    
    async def execute(self, context: Context) -> StateResult:
        """Upload file to Slack."""
        # Get configuration
        channels = self.config["channels"]
        content = self.config["content"]
        filename = self.config["filename"]
        title = self.config.get("title")
        initial_comment = self.config.get("initial_comment")
        token = self.config.get("token") or context.get_secret("slack_token")
        
        if not token:
            raise ValueError("Slack token is required")
        
        try:
            # Create client
            client = self._get_client(token)
            
            # Decode content if base64
            try:
                file_content = base64.b64decode(content)
            except:
                file_content = content.encode() if isinstance(content, str) else content
            
            # Upload file
            response = await client.files_upload_v2(
                channels=channels,
                content=file_content,
                filename=filename,
                title=title or filename,
                initial_comment=initial_comment
            )
            
            # Store outputs
            file_info = response["file"]
            context.set_output("file_id", file_info["id"])
            context.set_output("url", file_info.get("url_private", file_info.get("permalink")))
            
        except SlackApiError as e:
            raise RuntimeError(f"Slack API error: {e.response['error']}")
        except Exception as e:
            raise RuntimeError(f"Failed to upload file: {str(e)}")
        
        return None
    
    def validate_inputs(self, context: Context) -> None:
        """Validate required inputs."""
        if not self.config.get("channels"):
            raise ValueError("Channels are required")
        
        if not self.config.get("content"):
            raise ValueError("File content is required")
        
        if not self.config.get("filename"):
            raise ValueError("Filename is required")


class CreateChannelState(SlackBaseState):
    """Create a new Slack channel."""
    
    async def execute(self, context: Context) -> StateResult:
        """Create Slack channel."""
        # Get configuration
        name = self.config["name"]
        is_private = self.config.get("is_private", False)
        description = self.config.get("description")
        token = self.config.get("token") or context.get_secret("slack_token")
        
        if not token:
            raise ValueError("Slack token is required")
        
        try:
            # Create client
            client = self._get_client(token)
            
            # Create channel
            response = await client.conversations_create(
                name=name,
                is_private=is_private
            )
            
            channel_id = response["channel"]["id"]
            
            # Set description if provided
            if description:
                await client.conversations_setPurpose(
                    channel=channel_id,
                    purpose=description
                )
            
            # Store outputs
            context.set_output("channel_id", channel_id)
            context.set_output("channel_name", response["channel"]["name"])
            
            # Store for future use
            context.set_state("created_channel_id", channel_id)
            
        except SlackApiError as e:
            if e.response['error'] == 'name_taken':
                # Channel already exists, try to find it
                response = await client.conversations_list(limit=1000)
                for channel in response["channels"]:
                    if channel["name"] == name:
                        context.set_output("channel_id", channel["id"])
                        context.set_output("channel_name", channel["name"])
                        return None
                
            raise RuntimeError(f"Slack API error: {e.response['error']}")
        except Exception as e:
            raise RuntimeError(f"Failed to create channel: {str(e)}")
        
        return None
    
    def validate_inputs(self, context: Context) -> None:
        """Validate required inputs."""
        if not self.config.get("name"):
            raise ValueError("Channel name is required")
        
        # Validate channel name format
        name = self.config["name"]
        if not name.replace("-", "").replace("_", "").isalnum():
            raise ValueError("Channel name must contain only letters, numbers, hyphens, and underscores")
        
        if len(name) > 80:
            raise ValueError("Channel name must be 80 characters or less")