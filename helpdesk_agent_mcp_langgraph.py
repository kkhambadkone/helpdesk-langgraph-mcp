"""
Helpdesk Agent — MCP + LangGraph version
Classifies a problem with an LLM, then orchestrates Jira / Slack / Email
MCP servers through a LangGraph StateGraph instead of calling each API directly.

Install:
    pip install "langchain[mcp]>=1.4.0" langchain-ollama fastmcp langgraph pydantic

Run (mock mode, no live credentials needed):
    USE_MOCKS=true python helpdesk_agent_mcp_langgraph.py

Run (live — requires Atlassian OAuth, Slack OAuth, and the email_mcp_server.py
in the same directory):
    USE_MOCKS=false python helpdesk_agent_mcp_langgraph.py

Auth note: Atlassian's and Slack's hosted MCP servers both require OAuth 2.1.
The standalone `langchain_mcp_adapters` package does NOT perform OAuth for SSE/
HTTP connections — pointing it at a bare URL sends an unauthenticated request
and gets a 401. This version instead uses the newer `langchain.mcp.MCPAdapter`,
which delegates auth to FastMCP and supports `auth="oauth"`: full OAuth 2.1
discovery, dynamic client registration, and the browser consent flow,
automatically, on first connection.
"""

import asyncio
import operator
import os
from functools import partial
from typing import Annotated, Optional, TypedDict

try:
    from dotenv import load_dotenv

    load_dotenv()  # loads .env from the current directory into os.environ, if present
except ImportError:
    pass  # dotenv not installed — env vars must be exported manually instead

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

# Atlassian deprecated the SSE endpoint (https://mcp.atlassian.com/v1/sse) as
# of June 30, 2026, in favor of Streamable HTTP. Using the /v1/mcp endpoint.
ATLASSIAN_MCP_URL = "https://mcp.atlassian.com/v1/mcp"
SLACK_MCP_URL = "https://mcp.slack.com/mcp"

# Slack's MCP server does NOT support Dynamic Client Registration, unlike
# Atlassian's — attempting auth="oauth" (which relies on DCR) against Slack
# fails with something like:
#   RuntimeError: Client failed to connect: Registration failed: 302
#   (redirected to https://mcp-<id>.slack.com/register; not followed)
# The fix is a pre-registered OAuth app: create one at api.slack.com/apps,
# enable "Model Context Protocol" under Agents & AI Apps, and add a redirect
# URL matching SLACK_OAUTH_CALLBACK_PORT below exactly:
#   http://127.0.0.1:<SLACK_OAUTH_CALLBACK_PORT>/callback
SLACK_OAUTH_CALLBACK_PORT = int(os.environ.get("SLACK_OAUTH_CALLBACK_PORT", "8732"))
SLACK_MCP_CLIENT_ID = os.environ.get("SLACK_MCP_CLIENT_ID", "<SLACK_MCP_CLIENT_ID>")
SLACK_MCP_CLIENT_SECRET = os.environ.get("SLACK_MCP_CLIENT_SECRET")  # optional (PKCE)

# By default FastMCP's OAuth helper holds tokens in memory only, so every run
# (a fresh Python process) re-triggers the browser consent flow. Persisting
# tokens to disk avoids that. Requires: pip install "py-key-value-aio[disk]" cryptography
OAUTH_STATE_DIR = os.path.expanduser("~/.helpdesk-agent")
OAUTH_TOKEN_DIR = os.path.join(OAUTH_STATE_DIR, "oauth-tokens")
OAUTH_KEY_FILE = os.path.join(OAUTH_STATE_DIR, "oauth-key")


def _get_or_create_encryption_key() -> bytes:
    """Reuse a key from OAUTH_STORAGE_ENCRYPTION_KEY if set; otherwise
    generate one once and persist it locally so tokens stay decryptable
    across runs. Losing this file just means re-authenticating, not
    losing access to anything — it only protects the local token cache."""
    env_key = os.environ.get("OAUTH_STORAGE_ENCRYPTION_KEY")
    if env_key:
        return env_key.encode()

    os.makedirs(OAUTH_STATE_DIR, exist_ok=True)
    if os.path.exists(OAUTH_KEY_FILE):
        with open(OAUTH_KEY_FILE, "rb") as f:
            return f.read()

    from cryptography.fernet import Fernet

    key = Fernet.generate_key()
    with open(OAUTH_KEY_FILE, "wb") as f:
        f.write(key)
    print(f"[oauth] Generated a new token encryption key at {OAUTH_KEY_FILE}.")
    return key


def _build_token_storage():
    from cryptography.fernet import Fernet
    from key_value.aio.stores.disk import DiskStore
    from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

    return FernetEncryptionWrapper(
        key_value=DiskStore(directory=OAUTH_TOKEN_DIR),
        fernet=Fernet(_get_or_create_encryption_key()),
    )


def build_mcp_client_group():
    """Build a FastMCP ClientGroup: Atlassian authenticates via automatic
    OAuth 2.1 + Dynamic Client Registration (browser consent on first use);
    Slack authenticates via a pre-registered OAuth client, since its server
    doesn't support DCR; email is a local stdio subprocess with no auth.
    Both OAuth clients share one encrypted, on-disk token store, so consent
    only needs to happen once rather than on every run. Only called in live
    (non-mock) mode."""
    from fastmcp import Client, ClientGroup
    from fastmcp.client.auth import OAuth
    from fastmcp.client.transports import StdioTransport

    email_transport = StdioTransport(
        command="python",
        args=[os.path.join(os.path.dirname(__file__), "email_mcp_server.py")],
        env={
            "SMTP_SERVER": os.environ.get("SMTP_SERVER", "smtp.gmail.com"),
            "SMTP_PORT": os.environ.get("SMTP_PORT", "587"),
            "EMAIL_USER": os.environ.get("EMAIL_USER", "<EMAIL_USER>"),
            "EMAIL_PASS": os.environ.get("EMAIL_PASS", "<EMAIL_PASS>"),
        },
    )

    token_storage = _build_token_storage()

    atlassian_auth = OAuth(token_storage=token_storage)
    slack_auth = OAuth(
        client_id=SLACK_MCP_CLIENT_ID,
        client_secret=SLACK_MCP_CLIENT_SECRET,
        callback_port=SLACK_OAUTH_CALLBACK_PORT,
        token_storage=token_storage,
    )

    return ClientGroup(
        {
            "atlassian": Client(ATLASSIAN_MCP_URL, auth=atlassian_auth),
            "slack": Client(SLACK_MCP_URL, auth=slack_auth),
            "email": Client(email_transport),
        }
    )

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
    notifications_sent: Annotated[list, operator.add]


# ==============================================================================
# MOCK TOOLS — used when USE_MOCKS=true, so the graph can be exercised with
# zero external dependencies (same pattern as resilient-agent-platform).
# ==============================================================================


# ==============================================================================
# TOOL NAMES — ClientGroup/MCPAdapter automatically prefixes every tool with
# its server key ("atlassian", "slack", "email" in build_mcp_client_group()),
# so e.g. Atlassian's "getAccessibleAtlassianResources" becomes
# "atlassian_getAccessibleAtlassianResources" once loaded through the group.
# Slack's own tool is already named "slack_send_message", so prefixed it
# becomes the doubled-up "slack_slack_send_message" — easy to miss, and the
# actual cause of an earlier KeyError in this project. Mocks use these same
# prefixed names so mock and live mode can't silently diverge again.
# ==============================================================================

TOOL_GET_CLOUD_ID = "atlassian_getAccessibleAtlassianResources"
TOOL_CREATE_TICKET = "atlassian_createJiraIssue"
TOOL_SEND_SLACK = "slack_slack_send_message"
TOOL_SEND_EMAIL = "email_send_email"


class _MockTool:
    def __init__(self, name):
        self.name = name

    async def ainvoke(self, args: dict):
        print(f"[MOCK] {self.name} called with {args}")
        if self.name == TOOL_GET_CLOUD_ID:
            import json

            return [
                {"type": "text", "text": "[MOCK NOTICE] some unrelated informational block", "id": "lc_mock-notice"},
                {
                    "type": "text",
                    "text": json.dumps(
                        [{"id": "mock-cloud-id", "url": JIRA_SITE_URL.rstrip("/"), "scopes": ["read:jira-work", "write:jira-work"]}]
                    ),
                    "id": "lc_mock-resources",
                },
            ]
        if self.name == TOOL_CREATE_TICKET:
            return {"key": "MOCK-123"}
        if self.name == TOOL_SEND_SLACK:
            return {"ok": True, "ts": "0000.0001"}
        if self.name == TOOL_SEND_EMAIL:
            return "Email sent (mock)"
        return {}


def load_mock_tools() -> dict:
    names = [TOOL_GET_CLOUD_ID, TOOL_CREATE_TICKET, TOOL_SEND_SLACK, TOOL_SEND_EMAIL]
    return {name: _MockTool(name) for name in names}


# ==============================================================================
# GRAPH NODES
# ==============================================================================


async def classify_node(state: AgentState, tools: dict) -> dict:
    decision = await _classify_chain.ainvoke(
        {"problem": state["problem"], "format_instructions": _parser.get_format_instructions()}
    )
    if isinstance(decision, TicketDecision):
        decision = decision.model_dump()
    print("AI Decision:", decision)
    return {"decision": decision}


def _extract_atlassian_cloud_id(raw_response, jira_site_url: str):
    """getAccessibleAtlassianResources can return several text content blocks
    (e.g. an informational notice plus the actual resource list) — the real
    resource array isn't necessarily block [0]. Scan every block, find the
    one that parses as a list of resource dicts (has a "scopes" field), then
    pick the entry matching our configured site with Jira access specifically
    (a site can have separate resource entries for Jira vs Confluence scopes)."""
    import json

    if not isinstance(raw_response, list):
        return None

    resources = None
    for block in raw_response:
        text = block.get("text") if isinstance(block, dict) else None
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict) and "scopes" in parsed[0]:
            resources = parsed
            break

    if not resources:
        return None

    target = jira_site_url.rstrip("/")
    for r in resources:
        if r.get("url", "").rstrip("/") == target and any("jira" in s for s in r.get("scopes", [])):
            return r.get("id")
    for r in resources:  # fall back to any match on the site, any scope
        if r.get("url", "").rstrip("/") == target:
            return r.get("id")
    return resources[0].get("id")  # last resort


def _parse_mcp_result(result):
    """MCP tool results sometimes come back as a plain dict, and sometimes
    as a list of content blocks (e.g. [{'type': 'text', 'text': '...json...'}])
    depending on the server/adapter. Normalize to a plain dict either way."""
    import json

    if isinstance(result, dict):
        return result
    if isinstance(result, list) and result and isinstance(result[0], dict) and "text" in result[0]:
        try:
            return json.loads(result[0]["text"])
        except (json.JSONDecodeError, TypeError):
            return {"raw": result[0]["text"]}
    return {"raw": result}


async def resolve_cloud_id_node(state: AgentState, tools: dict) -> dict:
    resources = await tools[TOOL_GET_CLOUD_ID].ainvoke({})
    print("[debug] getAccessibleAtlassianResources raw response:", resources)
    cloud_id = _extract_atlassian_cloud_id(resources, JIRA_SITE_URL)
    if cloud_id is None:
        raise RuntimeError(
            f"Could not find an Atlassian resource matching JIRA_SITE_URL={JIRA_SITE_URL!r} "
            f"in the response above — check the site URL is correct and includes Jira scopes."
        )
    return {"cloud_id": cloud_id}


async def create_ticket_node(state: AgentState, tools: dict) -> dict:
    decision = state["decision"]
    result = await tools[TOOL_CREATE_TICKET].ainvoke(
        {
            "cloudId": state["cloud_id"],
            "projectKey": JIRA_PROJECT_KEY,
            "issueTypeName": decision["issue_type"],
            "summary": decision["summary"][:255],
            "description": decision["description"],
        }
    )
    parsed = _parse_mcp_result(result)
    if parsed.get("error"):
        raise RuntimeError(f"createJiraIssue failed: {parsed.get('message', parsed)}")
    ticket_key = parsed.get("key") or parsed.get("raw") or str(parsed)
    print("Jira ticket created:", ticket_key)
    return {"ticket_key": ticket_key}


async def notify_email_node(state: AgentState, tools: dict) -> dict:
    await tools[TOOL_SEND_EMAIL].ainvoke(
        {
            "to": NOTIFY_EMAIL_TO,
            "subject": f"Helpdesk Ticket {state['ticket_key']}",
            "body": f"New Jira ticket created: {state['ticket_key']}\n\nIssue: {state['problem']}",
        }
    )
    return {"notifications_sent": ["email"]}


async def notify_slack_node(state: AgentState, tools: dict) -> dict:
    await tools[TOOL_SEND_SLACK].ainvoke(
        {
            "channel_id": SLACK_CHANNEL_ID,
            "message": f"🚨 New Helpdesk Ticket Created\nTicket: {state['ticket_key']}\nIssue: {state['problem']}",
        }
    )
    return {"notifications_sent": ["slack"]}


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

    graph.add_node("classify", partial(classify_node, tools=tools))
    graph.add_node("resolve_cloud_id", partial(resolve_cloud_id_node, tools=tools))
    graph.add_node("create_ticket", partial(create_ticket_node, tools=tools))
    graph.add_node("notify_email", partial(notify_email_node, tools=tools))
    graph.add_node("notify_slack", partial(notify_slack_node, tools=tools))

    graph.set_entry_point("classify")
    graph.add_edge("classify", "resolve_cloud_id")
    graph.add_edge("resolve_cloud_id", "create_ticket")
    graph.add_conditional_edges(
        "create_ticket", route_after_ticket, ["notify_email", "notify_slack", END]
    )
    graph.add_edge("notify_email", END)
    graph.add_edge("notify_slack", END)

    return graph.compile()


async def run_live(problem: str) -> dict:
    from langchain.mcp import MCPAdapter

    group = build_mcp_client_group()
    async with MCPAdapter(group) as adapter:
        tool_list = await adapter.list_tools()
        tools = {tool.name: tool for tool in tool_list}
        app = build_graph(tools)
        return await app.ainvoke(
            {
                "problem": problem,
                "decision": None,
                "cloud_id": None,
                "ticket_key": None,
                "notifications_sent": [],
            }
        )


async def run_mock(problem: str) -> dict:
    tools = load_mock_tools()
    app = build_graph(tools)
    return await app.ainvoke(
        {
            "problem": problem,
            "decision": None,
            "cloud_id": None,
            "ticket_key": None,
            "notifications_sent": [],
        }
    )


async def main():
    print("Describe the problem: ")
    lines = []
    while True:
        line = input()
        if line == "":
            break
        lines.append(line)
    problem = "\n".join(lines)

    final_state = await (run_mock(problem) if USE_MOCKS else run_live(problem))
    print("\nFinal state:", final_state)


if __name__ == "__main__":
    asyncio.run(main())
