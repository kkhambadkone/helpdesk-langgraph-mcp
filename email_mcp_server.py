"""
email_mcp_server.py — minimal MCP server that wraps SMTP send-mail as a tool.

Run standalone for testing:
    python email_mcp_server.py

Install:
    pip install fastmcp
"""

import os
import smtplib
from email.mime.text import MIMEText

from fastmcp import FastMCP

mcp = FastMCP("email-server")

SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
EMAIL_USER = os.environ.get("EMAIL_USER", "<EMAIL_USER>")
EMAIL_PASS = os.environ.get("EMAIL_PASS", "<EMAIL_PASS>")  # Gmail App Password


@mcp.tool()
def send_email(to: str, subject: str, body: str) -> str:
    """
    Send an email notification.

    Args:
        to: recipient email address
        subject: email subject line
        body: plain-text email body
    Returns:
        Confirmation string.
    """
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = EMAIL_USER
    msg["To"] = to

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(EMAIL_USER, EMAIL_PASS)
        server.send_message(msg)

    return f"Email sent to {to} with subject '{subject}'"


if __name__ == "__main__":
    # stdio transport — matches how the orchestrator's MultiServerMCPClient
    # will launch this as a subprocess.
    mcp.run(transport="stdio")
