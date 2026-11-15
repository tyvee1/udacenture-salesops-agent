"""Email drafting and the simulated outbox.

Drafting is low risk: it reads the CRM and returns text. Sending is the
high-risk action. "Sending" appends a record to a local JSONL outbox, as the
prototype did, but the outbox now lives under the runtime ``state/`` directory
instead of ``data/``, so running the agent never modifies its input data.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from salesops_agent.data import AccountNotFound


class EmailOutbox:
    """Append-only JSONL file standing in for an email provider.

    One JSON object per line. Appending a line is the closest a local file gets
    to an atomic send: a crash mid-write leaves at most one partial last line,
    never a corrupted array.
    """

    def __init__(self, path: Path):
        self.path = path

    def deliver(self, message: dict[str, Any]) -> dict[str, Any]:
        """Record ``message`` as sent and return the stored record."""
        record = {**message, "status": "sent", "sent_at": datetime.now(UTC).isoformat()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        return record

    def count(self) -> int:
        """Number of messages recorded as sent (used to prove a rejection sent nothing)."""
        if not self.path.is_file():
            return 0
        return sum(1 for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip())


def draft_email(ctx, account_name_or_id: str, purpose: str) -> dict[str, Any]:
    """Draft a follow-up to the account's primary contact (falls back to the first contact)."""
    try:
        account = ctx.crm.find_account(account_name_or_id)
    except AccountNotFound as exc:
        return {"error": str(exc), "candidates": exc.candidates}
    contacts = ctx.crm.contacts_for(account["account_id"])
    contact = next((c for c in contacts if c.get("is_primary")), contacts[0] if contacts else None)
    if contact is None:
        return {"error": f"No contact found for account {account['name']}"}
    return {
        "status": "drafted",
        "account_id": account["account_id"],
        "to": contact["email"],
        "subject": f"Following up on {account['name']}",
        "body": (
            f"Hi {contact['first_name']},\n\n"
            f"I wanted to follow up regarding {purpose}.\n\n"
            "Best regards,\nThe UdaCenture Sales Team\n"
        ),
    }


def send_email(ctx, to: str, subject: str, body: str) -> dict[str, Any]:
    """Send an email by appending it to the outbox."""
    record = ctx.outbox.deliver({"to": to, "subject": subject, "body": body})
    return {"status": "sent", "message": record}
