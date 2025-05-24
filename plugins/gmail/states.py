"""Gmail plugin states implementation."""

import base64
from typing import Dict, Any, List, Optional
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.encoders import encode_base64

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from core.plugins.base import PluginState
from core.agent.context import Context
from core.agent.state import StateResult


class GmailBaseState(PluginState):
    """Base state for Gmail operations."""
    
    def _get_service(self, credentials: Dict[str, Any]):
        """Create Gmail service instance."""
        creds = Credentials(
            token=credentials.get("token"),
            refresh_token=credentials.get("refresh_token"),
            token_uri=credentials.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=credentials.get("client_id"),
            client_secret=credentials.get("client_secret")
        )
        
        return build('gmail', 'v1', credentials=creds)


class SendEmailState(GmailBaseState):
    """Send an email via Gmail."""
    
    async def execute(self, context: Context) -> StateResult:
        """Send email."""
        # Get configuration
        to_addresses = self.config["to"]
        subject = self.config["subject"]
        body = self.config["body"]
        cc_addresses = self.config.get("cc", [])
        bcc_addresses = self.config.get("bcc", [])
        attachments = self.config.get("attachments", [])
        credentials = self.config.get("credentials") or context.get_secret("gmail_credentials")
        
        if not credentials:
            raise ValueError("Gmail credentials are required")
        
        try:
            # Create service
            service = self._get_service(credentials)
            
            # Create message
            if attachments:
                message = MIMEMultipart()
                message.attach(MIMEText(body, 'plain'))
                
                # Add attachments
                for attachment in attachments:
                    part = MIMEBase('application', 'octet-stream')
                    part.set_payload(base64.b64decode(attachment['content']))
                    encode_base64(part)
                    part.add_header(
                        'Content-Disposition',
                        f'attachment; filename="{attachment["filename"]}"'
                    )
                    message.attach(part)
            else:
                message = MIMEText(body)
            
            # Set headers
            message['to'] = ', '.join(to_addresses)
            message['subject'] = subject
            
            if cc_addresses:
                message['cc'] = ', '.join(cc_addresses)
            if bcc_addresses:
                message['bcc'] = ', '.join(bcc_addresses)
            
            # Send message
            raw_message = base64.urlsafe_b64encode(
                message.as_bytes()
            ).decode('utf-8')
            
            result = service.users().messages().send(
                userId='me',
                body={'raw': raw_message}
            ).execute()
            
            # Store outputs
            context.set_output("message_id", result['id'])
            context.set_output("thread_id", result.get('threadId'))
            
        except HttpError as e:
            raise RuntimeError(f"Gmail API error: {str(e)}")
        except Exception as e:
            raise RuntimeError(f"Failed to send email: {str(e)}")
        
        return None
    
    def validate_inputs(self, context: Context) -> None:
        """Validate required inputs."""
        if not self.config.get("to"):
            raise ValueError("To addresses are required")
        
        if not self.config.get("subject"):
            raise ValueError("Subject is required")
        
        if not self.config.get("body"):
            raise ValueError("Body is required")


class ReadEmailsState(GmailBaseState):
    """Read emails from Gmail."""
    
    async def execute(self, context: Context) -> StateResult:
        """Read emails."""
        # Get configuration
        query = self.config.get("query", "is:unread")
        max_results = self.config.get("max_results", 10)
        mark_as_read = self.config.get("mark_as_read", False)
        credentials = self.config.get("credentials") or context.get_secret("gmail_credentials")
        
        if not credentials:
            raise ValueError("Gmail credentials are required")
        
        try:
            # Create service
            service = self._get_service(credentials)
            
            # Search for messages
            results = service.users().messages().list(
                userId='me',
                q=query,
                maxResults=max_results
            ).execute()
            
            messages = results.get('messages', [])
            emails = []
            
            # Get full message details
            for msg in messages:
                message = service.users().messages().get(
                    userId='me',
                    id=msg['id']
                ).execute()
                
                # Parse email
                email_data = self._parse_message(message)
                emails.append(email_data)
                
                # Mark as read if requested
                if mark_as_read and 'UNREAD' in message.get('labelIds', []):
                    service.users().messages().modify(
                        userId='me',
                        id=msg['id'],
                        body={'removeLabelIds': ['UNREAD']}
                    ).execute()
            
            # Store outputs
            context.set_output("emails", emails)
            context.set_output("count", len(emails))
            
            # Store in context for processing
            context.set_state("fetched_emails", emails)
            
        except HttpError as e:
            raise RuntimeError(f"Gmail API error: {str(e)}")
        except Exception as e:
            raise RuntimeError(f"Failed to read emails: {str(e)}")
        
        return None
    
    def _parse_message(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """Parse Gmail message into structured format."""
        headers = {
            header['name']: header['value']
            for header in message['payload'].get('headers', [])
        }
        
        # Extract body
        body = self._get_message_body(message['payload'])
        
        return {
            "id": message['id'],
            "thread_id": message['threadId'],
            "from": headers.get('From', ''),
            "to": headers.get('To', ''),
            "subject": headers.get('Subject', ''),
            "date": headers.get('Date', ''),
            "body": body,
            "snippet": message.get('snippet', ''),
            "labels": message.get('labelIds', [])
        }
    
    def _get_message_body(self, payload: Dict[str, Any]) -> str:
        """Extract body from message payload."""
        body = ""
        
        if 'parts' in payload:
            for part in payload['parts']:
                if part['mimeType'] == 'text/plain':
                    data = part['body']['data']
                    body = base64.urlsafe_b64decode(data).decode('utf-8')
                    break
        elif payload['body'].get('data'):
            body = base64.urlsafe_b64decode(
                payload['body']['data']
            ).decode('utf-8')
        
        return body


class SearchEmailsState(GmailBaseState):
    """Search emails in Gmail."""
    
    async def execute(self, context: Context) -> StateResult:
        """Search emails."""
        # Get configuration
        query = self.config["query"]
        max_results = self.config.get("max_results", 50)
        include_body = self.config.get("include_body", True)
        credentials = self.config.get("credentials") or context.get_secret("gmail_credentials")
        
        if not credentials:
            raise ValueError("Gmail credentials are required")
        
        try:
            # Create service
            service = self._get_service(credentials)
            
            # Search messages
            all_messages = []
            page_token = None
            
            while len(all_messages) < max_results:
                results = service.users().messages().list(
                    userId='me',
                    q=query,
                    pageToken=page_token,
                    maxResults=min(max_results - len(all_messages), 100)
                ).execute()
                
                messages = results.get('messages', [])
                all_messages.extend(messages)
                
                page_token = results.get('nextPageToken')
                if not page_token:
                    break
            
            # Get message details if needed
            search_results = []
            for msg in all_messages[:max_results]:
                if include_body:
                    message = service.users().messages().get(
                        userId='me',
                        id=msg['id']
                    ).execute()
                    result = self._parse_message(message)
                else:
                    # Just basic info
                    result = {
                        "id": msg['id'],
                        "thread_id": msg.get('threadId')
                    }
                
                search_results.append(result)
            
            # Store outputs
            context.set_output("results", search_results)
            context.set_output("total", len(search_results))
            
        except HttpError as e:
            raise RuntimeError(f"Gmail API error: {str(e)}")
        except Exception as e:
            raise RuntimeError(f"Failed to search emails: {str(e)}")
        
        return None
    
    def validate_inputs(self, context: Context) -> None:
        """Validate required inputs."""
        if not self.config.get("query"):
            raise ValueError("Search query is required")