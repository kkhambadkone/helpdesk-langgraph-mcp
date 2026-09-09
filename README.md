# AI Helpdesk Agent (LangGraph + MCP)

An **AI-powered helpdesk agent** that automatically analyzes user-reported issues using an LLM and performs automated incident management actions such as:

* Creating **Jira tickets**
* Sending **email notifications**
* Posting **Slack alerts**

The agent uses **Llama3 via Ollama** to understand problem descriptions and determine the appropriate **priority** and **issue type**, then orchestrates the resulting actions through **MCP (Model Context Protocol) servers** wired together with a **LangGraph StateGraph**.

---

# Features

* 🧠 **AI-powered issue classification**
* 🎟️ **Automatic Jira ticket creation** via Atlassian's hosted MCP server
* 📨 **Email notification to support teams** via a custom local MCP server
* 💬 **Slack alerts for real-time monitoring** via Slack's hosted MCP server
* ⚡ **Fully automated helpdesk workflow**, with email/Slack notifications fanning out in parallel
* 🔔 **Demonstrates 2-shot and chain-of-thought prompting**
* 🔗 **Demonstrates LangChain + LangGraph + MCP together**
* 🧪 **Mock mode** — exercise the full graph with zero live credentials

---

# Architecture

```
Problem
   ↓
classify (LLM, chain-of-thought prompt)
   ↓
resolve_cloud_id (Atlassian MCP)
   ↓
create_ticket (Atlassian MCP)
   ↓
 ┌────────────┬────────────┐
 ▼            ▼            ▼
(none)   notify_email   notify_slack
             (MCP)         (MCP)
```

The AI agent analyzes the issue and automatically decides:

* Issue **priority** (Low, Medium, High)
* Issue **type** (Bug, Task, Story)
* Whether to send **email notifications**
* Whether to send **Slack alerts**

Jira and Slack are both accessed through their official **hosted** MCP servers — remote, OAuth-based, nothing to install or run locally for either. Email has no equivalent off-the-shelf MCP server, so this project includes a small custom one (`email_mcp_server.py`) that runs locally.

---

# Example Workflow

**Input:**

```
Production website is down and users cannot log in.
```

**AI Decision:**

```
Priority: High
Issue Type: Bug
Slack Alert: Yes
Email Notification: Yes
```

**Actions Performed:**

```
1. Jira ticket created
2. Email notification sent
3. Slack alert posted
```


**Input:**

```
Create a product selection page where users can choose:
 - Product category from a dropdown
 - Product brand from a dropdown
 - Delivery speed using radio buttons
 - Warranty option using radio buttons
```

**AI Decision:**

```
Priority: Medium 
Issue Type: Story 
Slack Alert: No 
Email Notification: No 
```

**Actions Performed:**

```
1. Jira ticket created
```

---

# Files

* `helpdesk_agent_mcp_langgraph.py` — main orchestrator
* `email_mcp_server.py` — local MCP server wrapping SMTP send-mail (no off-the-shelf email MCP server exists, so this one is custom-built for the project)
* `requirements.txt` — dependencies

---

# Requirements

* Python 3.9+
* Ollama
* Jira Cloud account
* Slack workspace
* SMTP email account

Install dependencies:

```
pip install -r requirements.txt
```

Installs: `langchain`, `langchain-ollama`, `langchain-mcp-adapters`, `langgraph`, `pydantic`, `fastmcp`.

No Node.js or `npx` is required — both Jira and Slack are accessed through their official hosted MCP servers.

---

# Running the LLM

Install Ollama and run Llama3 locally.

Install Ollama:

https://ollama.com

Pull the model:

```
ollama pull llama3
```

Start the Ollama service.

---

# Jira Configuration

The agent talks to **Atlassian's official Rovo MCP server** (`https://mcp.atlassian.com/v1/sse`), which authenticates via **OAuth 2.1** (or an API token, if an org admin has enabled that method) and scopes access to whatever Jira projects your account can already see.

* No API token is stored in the script.
* The agent calls `getAccessibleAtlassianResources` at the start of each run to resolve your Atlassian **cloudId**, then passes that into `createJiraIssue`.
* Set the project key as an environment variable:

```
export JIRA_PROJECT_KEY="SCRUM"
export JIRA_SITE_URL="https://your-domain.atlassian.net"
```

* First run will trigger an OAuth consent flow — this needs to be completed interactively at least once.
* **Still to confirm:** the exact required/optional fields `createJiraIssue` expects (in particular, whether `description` needs to be pre-formatted as Atlassian Document Format rather than plain text) — check `tool.args_schema` after calling `client.get_tools()`.

---

# Slack Configuration

Slack ships its own first-party remote MCP server, hosted at `https://mcp.slack.com/mcp`, generally available since February 17, 2026. Like the Atlassian server, it's remote and OAuth-based: JSON-RPC 2.0 over Streamable HTTP, authenticating individual users through OAuth 2.0 with granular scopes rather than a shared bot token, and it inherits the authenticating user's own Slack permissions rather than a separate service account.

* No bot token, no `npx` package, nothing local to install for Slack.
* A workspace admin must approve the connection before it can be used the first time.
* Set the target channel:

```
export SLACK_CHANNEL_ID="C0XXXXXXX"
```

* First run triggers an OAuth consent flow, same as Atlassian — needs to be completed interactively at least once.

(There is also a locally-run alternative — `@modelcontextprotocol/server-slack` — but it's Anthropic's archived reference server: an early local server configured with Slack bot tokens, no longer patched. A maintained community alternative, korotovsky/slack-mcp-server, self-hosted via npx or Docker, exists if a local deployment is ever preferred over the hosted one. This project uses the official hosted server.)

---

# Email MCP Server Configuration

SMTP settings are supplied as environment variables and consumed by `email_mcp_server.py`:

```
export SMTP_SERVER="smtp.gmail.com"
export SMTP_PORT="587"
export EMAIL_USER="your_email@gmail.com"
export EMAIL_PASS="your_app_password"
export NOTIFY_EMAIL_TO="support-team@example.com"
```

`EMAIL_PASS` must be a Gmail **App Password** (requires 2-Step Verification enabled on the account), not your normal Gmail login password.

---

# Running the Email MCP Server in the Background (nohup)

Of the three MCP servers, **email is the only local process** — Jira and Slack are both remote/hosted, so there's nothing to background for either of those.

In the live (non-mock) configuration, `helpdesk_agent_mcp_langgraph.py` launches `email_mcp_server.py` itself as a subprocess over stdio — you don't normally start it separately. If you want it running persistently in the background instead (e.g. to test it standalone, or reuse it across multiple orchestrator runs), use `nohup`:

```
nohup python email_mcp_server.py > email_mcp_server.log 2>&1 &
```

* `nohup` keeps the process alive after the terminal session ends
* `> email_mcp_server.log 2>&1` redirects stdout/stderr to a log file, since the server doesn't print anything during normal operation
* `&` backgrounds it

Check it's running:

```
ps aux | grep email_mcp_server.py
```

Stop it:

```
kill <pid>
```

**Still missing:** whether the OAuth session/token for the Atlassian and Slack remote servers is cached to disk between runs (so re-authentication isn't required every single execution) hasn't been verified against `langchain-mcp-adapters`' behavior.

---

# Running the Program

Mock mode — exercises the full LangGraph flow with no live Jira/Slack/email credentials, using stubbed tool responses:

```
USE_MOCKS=true python helpdesk_agent_mcp_langgraph.py
```

Live mode — requires all of the Jira, Slack, and email configuration above to be set:

```
USE_MOCKS=false python helpdesk_agent_mcp_langgraph.py
```

Enter the problem description at the prompt and press Enter twice to submit:

```
Describe the problem: The following features have to be added to the application 1. Add Dropdown to select city and state 2. Add checkbox to accept terms. In ticket Description create a neatly formatted list numbered list of the features. This is not a bug. Create it with issuetype of Feature. Keep summary short to not exceed 50 characters.
```

**Still missing:** no worked example output for this version yet (a transcript showing the AI decision, ticket creation, and notification results end to end), and no example screenshots for the LangGraph flow.

---

# Example Output

Analyzing issue using AI...

AI Decision: {'priority': 'Low', 'issue_type': 'Feature', 'needs_slack': True, 'needs_email': False}
{'id': '10094', 'key': 'SCRUM-21', 'self': 'https://krish-ai-test.atlassian.net/rest/api/3/issue/10094'}
Jira ticket created: SCRUM-21
Slack notification sent

*(Console formatting shown above is from the earlier direct-API script — the LangGraph version prints a `final_state` dict instead — but the resulting Jira ticket, Slack message, and email are identical regardless of which implementation created them, so the screenshots below still apply.)*

---
## Example Jira Ticket


![Jira Ticket](screenshots/JIRA1.png)

<img src="screenshots/JIRA2.png" width="300" height="300">

## Example Slack channel notification

![Slack Channel Notificaiton](screenshots/SLACK1.png)

## Example Email 
                      
<img src="screenshots/EMAIL1.png" width="300" height="300">

---

# Future Enhancements

Possible improvements:

* Retrieval-Augmented Generation (RAG) using a **vector database**
* Incident knowledge base with **ChromaDB**
* Root cause suggestions based on historical incidents
* Automated remediation actions
* Integration with monitoring tools

---

# Technologies Used

* Python
* Ollama
* Llama3
* LangChain
* LangGraph
* MCP (Model Context Protocol) — Atlassian Rovo MCP server, Slack MCP server, custom email MCP server
* SMTP Email

---

# License

MIT License

---

# Author

Krishnanand Khambadkone

AI / Data / Cloud Enthusiast
