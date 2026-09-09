"""
Helpdesk Agent — MCP + LangGraph version
Classifies a problem with an LLM, then orchestrates Jira / Slack / Email
MCP servers through a LangGraph StateGraph instead of calling each API directly.

Install:
    pip install langchain langchain-ollama langchain-mcp-adapters langgraph pydantic

Run (mock mode, no live credentials needed):
    USE_MOCKS=true python helpdesk_agent_mcp_langgraph.py

Run (live — requires Atlassian OAuth/API-token config, a Slack bot token,
and the email_mcp_server.py in the same directory):
    USE_MOCKS=false python helpdesk_agent_mcp_langgraph.py
"""

import asyncio
import os
from typing import Optional, TypedDict

from pydantic import BaseModel, Field

from langchain_ollama import OllamaLLM
from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import JsonOutputParser
from langgraph.graph import StateGraph, END

# ==============================================================================
# CONFIGURATION
# ==============================================================================

MODEL = os.environ.get("HELPDESK_MODEL", "llama3")
USE_MOCKS = os.environ.get("USE_MOCKS", "true").lower() == "true"

JIRA_PROJECT_KEY = os.environ.get("JIRA_PROJECT_KEY", "<JIRA_PROJECT_KEY>")
JIRA_SITE_URL = os.environ.get("JIRA_SITE_URL", "<https://your-site.atlassian.net>")

SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID", "<SLACK_CHANNEL_ID>")
NOTIFY_EMAIL_TO = os.environ.get("NOTIFY_EMAIL_TO", "<EMAIL_TO>")

# Real MCP server configs — passed to langchain_mcp_adapters.MultiServerMCPClient.
# Atlassian's Rovo MCP server and Slack's official server are both remote/OAuth —
# nothing local to launch or background for either. Only the email server is a
# local process, built alongside this file.
MCP_SERVER_CONFIG = {
    "atlassian": {
        "transport": "sse",
        "url": "https://mcp.atlassian.com/v1/sse",
    },
    "slack": {
        "transport": "streamable_http",
        "url": "https://mcp.slack.com/mcp",
        # OAuth 2.0, not a bot token — handled via browser consent + workspace
        # admin approval on first connection, same pattern as Atlassian above.
    },
    "email": {
        "transport": "stdio",
        "command": "python",
        "args": [os.path.join(os.path.dirname(__file__), "email_mcp_server.py")],
        "env": {
            "SMTP_SERVER": os.environ.get("SMTP_SERVER", "smtp.gmail.com"),
            "SMTP_PORT": os.environ.get("SMTP_PORT", "587"),
            "EMAIL_USER": os.environ.get("EMAIL_USER", "<EMAIL_USER>"),
            "EMAIL_PASS": os.environ.get("EMAIL_PASS", "<EMAIL_PASS>"),
        },
    },
}

# ==============================================================================
# LANGCHAIN — structured output schema (unchanged from the original script)
# ==============================================================================


class TicketDecision(BaseModel):
    priority: str = Field(description="Low | Medium | High")
    issue_type: str = Field(description="Bug | Task | Story")
    needs_slack: bool = Field(description="True if Slack notification required")
    needs_email: bool = Field(description="True if email notification required")
    summary: str = Field(description="Short one-line title for the Jira ticket")
    description: str = Field(description="Full description (use \\n for newlines)")


_llm = OllamaLLM(model=MODEL)
_parser = JsonOutputParser(pydantic_object=TicketDecision)

_prompt = PromptTemplate(
    input_variables=["problem", "format_instructions"],
    template="""

You are an AI Helpdesk Agent for an engineering team that converts user problems into structured JIRA ticket metadata.

Follow these steps:

Step 1: Understand the problem.
Step 2: Determine if it is a Bug, Task, or Story.
Step 3: Determine priority based on impact.
Step 4: Decide if Slack notification is needed (urgent issues).
Step 5: Decide if Email notification is needed (stakeholder visibility).

Definitions:

Bug:
Something broken or not working.

Task:
Operational request or configuration change.

Story:
New feature request or enhancement.

Priority Rules:

High:

System outages, production failures, or security problems.

Medium:
Feature work or partial functionality issues.

Low:
Minor requests or documentation updates.

Classify the issue and return JSON with the following fields:

priority: Low | Medium | High
issue_type: Bug | Task | Story
needs_slack: true | false
needs_email: true | false
summary: short one-line title for the ticket
description: full description of the issue (use \\n for newlines, keep on a single line)


Example 1:
Here are an example of a Bug.

Problem:
"The company website shows a 500 error when users try to login."

Output:
{{
  "priority": "High",
  "issue_type": "Bug",
  "needs_slack": true,
  "needs_email": true,
  "summary": "User Login Issue",
  "description": "The company website shows a 500 error when users try to login."
}}

Example 2:
Here are an example of a Task.

Problem:
"The product page should allow users to select a product category and a brand from dropdowns, choose delivery speed using radio buttons, and select warranty options."

Output:
{{
  "priority": "Medium",
  "issue_type": "Story",
  "needs_slack": false,
  "needs_email": false,
  "summary": "Add selection features to product page",
  "description": "Include the following features:\\n1. Allow users to select a product category and a brand from dropdowns.\\n2. Choose delivery speed using radio buttons.\\n3. Select warranty options."
}}


Now analyze the following problem:

Problem:
{problem}

RULES:
1. Return ONLY valid JSON.
2. JSON must start with {{ and end with }}.
3. Include all fields:
   - priority (Low, Medium, High)
   - issue_type (Bug, Feature, Task)
   - needs_slack (true/false)
   - needs_email (true/false)
   - summary (short summary)
   - description (detailed description)
4. Escape all line breaks inside strings using \\n.
5. Do NOT include any extra text, explanations, or notes.

IMPORTANT: Output ONLY the raw JSON object. No preamble. No explanation. No markdown. Start with {{ and end with }}.
""",
)

_classify_chain = _prompt | _llm | _parser

# ==============================================================================
# GRAPH STATE
# ==============================================================================


class AgentState(TypedDict):
    problem: str
    decision: Optional[dict]
    cloud_id: Optional[str]
    ticket_key: Optional[str]
    notifications_sent: list


# ==============================================================================
# MOCK TOOLS — used when USE_MOCKS=true, so the graph can be exercised with
# zero external dependencies (same pattern as resilient-agent-platform).
# ==============================================================================


class _MockTool:
    def __init__(self, name):
        self.name = name

    async def ainvoke(self, args: dict):
        print(f"[MOCK] {self.name} called with {args}")
        if self.name == "getAccessibleAtlassianResources":
            return [{"id": "mock-cloud-id", "url": JIRA_SITE_URL}]
        if self.name == "createJiraIssue":
            return {"key": "MOCK-123"}
        if self.name == "slack_send_message":
            return {"ok": True, "ts": "0000.0001"}
        if self.name == "send_email":
            return "Email sent (mock)"
        return {}


async def load_tools() -> dict:
    """Return a dict of tool_name -> tool object, either mocked or loaded live
    from the MCP servers via langchain-mcp-adapters."""
    if USE_MOCKS:
        names = [
            "getAccessibleAtlassianResources",
            "createJiraIssue",
            "slack_send_message",
            "send_email",
        ]
        return {name: _MockTool(name) for name in names}

    from langchain_mcp_adapters.client import MultiServerMCPClient

    client = MultiServerMCPClient(MCP_SERVER_CONFIG)
    tool_list = await client.get_tools()
    return {tool.name: tool for tool in tool_list}


# ==============================================================================
# GRAPH NODES
# ==============================================================================


async def classify_node(state: AgentState, tools: dict) -> AgentState:
    decision = await _classify_chain.ainvoke(
        {"problem": state["problem"], "format_instructions": _parser.get_format_instructions()}
    )
    if isinstance(decision, TicketDecision):
        decision = decision.model_dump()
    print("AI Decision:", decision)
    state["decision"] = decision
    return state


async def resolve_cloud_id_node(state: AgentState, tools: dict) -> AgentState:
    resources = await tools["getAccessibleAtlassianResources"].ainvoke({})
    state["cloud_id"] = resources[0]["id"] if resources else None
    return state


async def create_ticket_node(state: AgentState, tools: dict) -> AgentState:
    decision = state["decision"]
    result = await tools["createJiraIssue"].ainvoke(
        {
            "cloudId": state["cloud_id"],
            "projectKey": JIRA_PROJECT_KEY,
            "issueTypeName": decision["issue_type"],
            "summary": decision["summary"][:255],
            "description": decision["description"],
        }
    )
    state["ticket_key"] = result.get("key") if isinstance(result, dict) else str(result)
    print("Jira ticket created:", state["ticket_key"])
    return state


async def notify_email_node(state: AgentState, tools: dict) -> AgentState:
    await tools["send_email"].ainvoke(
        {
            "to": NOTIFY_EMAIL_TO,
            "subject": f"Helpdesk Ticket {state['ticket_key']}",
            "body": f"New Jira ticket created: {state['ticket_key']}\n\nIssue: {state['problem']}",
        }
    )
    state.setdefault("notifications_sent", []).append("email")
    return state


async def notify_slack_node(state: AgentState, tools: dict) -> AgentState:
    await tools["slack_send_message"].ainvoke(
        {
            "channel_id": SLACK_CHANNEL_ID,
            "message": f"🚨 New Helpdesk Ticket Created\nTicket: {state['ticket_key']}\nIssue: {state['problem']}",
        }
    )
    state.setdefault("notifications_sent", []).append("slack")
    return state


def route_after_ticket(state: AgentState):
    branches = []
    decision = state["decision"]
    if decision.get("needs_email"):
        branches.append("notify_email")
    if decision.get("needs_slack"):
        branches.append("notify_slack")
    return branches or [END]


# ==============================================================================
# BUILD + RUN
# ==============================================================================


def build_graph(tools: dict):
    graph = StateGraph(AgentState)

    graph.add_node("classify", lambda s: classify_node(s, tools))
    graph.add_node("resolve_cloud_id", lambda s: resolve_cloud_id_node(s, tools))
    graph.add_node("create_ticket", lambda s: create_ticket_node(s, tools))
    graph.add_node("notify_email", lambda s: notify_email_node(s, tools))
    graph.add_node("notify_slack", lambda s: notify_slack_node(s, tools))

    graph.set_entry_point("classify")
    graph.add_edge("classify", "resolve_cloud_id")
    graph.add_edge("resolve_cloud_id", "create_ticket")
    graph.add_conditional_edges(
        "create_ticket", route_after_ticket, ["notify_email", "notify_slack", END]
    )
    graph.add_edge("notify_email", END)
    graph.add_edge("notify_slack", END)

    return graph.compile()


async def main():
    print("Describe the problem: ")
    lines = []
    while True:
        line = input()
        if line == "":
            break
        lines.append(line)
    problem = "\n".join(lines)

    tools = await load_tools()
    app = build_graph(tools)

    final_state = await app.ainvoke(
        {"problem": problem, "decision": None, "cloud_id": None, "ticket_key": None, "notifications_sent": []}
    )
    print("\nFinal state:", final_state)


if __name__ == "__main__":
    asyncio.run(main())
