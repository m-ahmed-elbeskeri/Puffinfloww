"""Example workflow using multiple integrations."""

import asyncio
from core import Agent, Context
from plugins.registry import plugin_registry


async def main():
    """Run an integration workflow example."""
    # Create agent
    agent = Agent("integration_example", max_concurrent=5)
    
    # Get plugins
    webhook_plugin = plugin_registry.get_plugin("webhook")
    openai_plugin = plugin_registry.get_plugin("openai")
    gmail_plugin = plugin_registry.get_plugin("gmail")
    slack_plugin = plugin_registry.get_plugin("slack")
    
    # Add webhook receiver state
    agent.add_state(
        "receive_webhook",
        webhook_plugin.get_state_function("webhook_receiver", {
            "endpoint": "/customer-inquiry",
            "timeout": 300
        })
    )
    
    # Add AI processing state
    agent.add_state(
        "analyze_inquiry",
        openai_plugin.get_state_function("chat_completion", {
            "model": "gpt-4",
            "messages": [
                {
                    "role": "system",
                    "content": "You are a customer service AI. Analyze the inquiry and determine the urgency level (low, medium, high) and suggest a response."
                },
                {
                    "role": "user",
                    "content": "{{webhook_payload}}"  # Will be replaced with actual payload
                }
            ],
            "temperature": 0.3
        }),
        dependencies={"receive_webhook": "required"}
    )
    
    # Add email notification state
    agent.add_state(
        "send_email_notification",
        gmail_plugin.get_state_function("send_email", {
            "to": ["support@example.com"],
            "subject": "New Customer Inquiry - {{urgency}} Priority",
            "body": "New inquiry received:\n\n{{inquiry_summary}}\n\nSuggested response:\n{{suggested_response}}"
        }),
        dependencies={"analyze_inquiry": "required"}
    )
    
    # Add Slack notification state
    agent.add_state(
        "notify_slack",
        slack_plugin.get_state_function("send_message", {
            "channel": "#customer-support",
            "text": "New customer inquiry received!",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "*Priority:* {{urgency}}\n*Summary:* {{inquiry_summary}}"
                    }
                }
            ]
        }),
        dependencies={"analyze_inquiry": "required"}
    )
    
    # Add response webhook state
    agent.add_state(
        "send_response",
        webhook_plugin.get_state_function("webhook_sender", {
            "url": "{{response_url}}",
            "payload": {
                "status": "received",
                "message": "Thank you for your inquiry. Our team will respond shortly.",
                "ticket_id": "{{ticket_id}}"
            }
        }),
        dependencies={
            "send_email_notification": "required",
            "notify_slack": "required"
        }
    )
    
    # Custom state to process AI response
    async def process_ai_response(context: Context):
        """Process the AI analysis results."""
        ai_response = context.get_output("response")
        
        # Parse AI response (in production, use proper parsing)
        # For demo, assume response contains urgency and suggested response
        context.set_state("urgency", "high")
        context.set_state("inquiry_summary", "Customer reporting service outage")
        context.set_state("suggested_response", "We're investigating the issue...")
        context.set_state("ticket_id", f"TKT-{int(time.time())}")
        
        return ["send_email_notification", "notify_slack"]
    
    agent.add_state(
        "process_ai_response",
        process_ai_response,
        dependencies={"analyze_inquiry": "required"}
    )
    
    # Run the workflow
    print("Starting integration workflow...")
    print("Waiting for webhook at /customer-inquiry...")
    
    try:
        await agent.run(timeout=600)  # 10 minute timeout
    except TimeoutError:
        print("No webhook received within timeout period")
    
    print("Workflow completed!")


if __name__ == "__main__":
    asyncio.run(main())