"""
inbox_agent.py — AI-powered email triage agent.

Scans an IMAP/SMTP inbox (or a mock provider in dry-run mode), classifies
each message as actionable vs noise, drafts intelligent replies using an LLM,
and routes messages to the appropriate folder.

Classification rules (in priority order):
  1. HARD-NOISE wins:  no-reply senders, automated keywords, alerts, newsletters
  2. TICKET emails:    subject contains "[Ticket #…]"  → always draft
  3. PARTNER emails:   known trading partner domains    → always draft
  4. INTERNAL emails:  sender matches COMPANY_EMAIL domain
  5. Everything else:  silently filed / ignored

The agent is intentionally email-provider agnostic.  Swap the
``_fetch_messages`` method to plug in Outlook (via pywin32), Gmail,
or any IMAP source.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..config import config
from ..utils.message_bus import MessageBus
from ..utils.retry import llm_retry

log = logging.getLogger("firstpass.inbox_agent")

# ── Noise filters ─────────────────────────────────────────────────────────────

_NOISE_SENDER_PATTERNS = re.compile(
    r"no.?reply|noreply|donotreply|mailer.daemon|postmaster|"
    r"automated|notification|alert|newsletter|unsubscribe|bounce",
    re.I,
)

_NOISE_SUBJECT_KEYWORDS = re.compile(
    r"unsubscribe|auto.?reply|out of office|delivery status|"
    r"order shipped|tracking update|invoice attached|statement ready|"
    r"price list|promotion|deal|offer expires",
    re.I,
)

_TICKET_SUBJECT_PATTERN = re.compile(r"\[ticket\s*#?\d+\]", re.I)

# ── Message model ─────────────────────────────────────────────────────────────

class EmailMessage:
    def __init__(self, msg_id: str, subject: str, sender: str, body: str,
                 received_at: Optional[str] = None, is_read: bool = False):
        self.msg_id = msg_id
        self.subject = subject
        self.sender = sender
        self.body = body
        self.received_at = received_at or datetime.now(timezone.utc).isoformat()
        self.is_read = is_read

    def __repr__(self) -> str:
        return f"<Email from={self.sender!r} subject={self.subject!r}>"


# ── Main agent ────────────────────────────────────────────────────────────────

class InboxAgent:
    """
    Email triage agent.

    In dry-run mode (no email provider configured) it processes a small set
    of synthetic demo messages so the pipeline can be exercised end-to-end.
    """

    def __init__(self):
        self.bus = MessageBus()
        self._company_domain = config.COMPANY_EMAIL.split("@")[-1]
        self._known_partner_domains: List[str] = []  # populated from partner registry

    # ── Public interface ──────────────────────────────────────────────────

    def run_cycle(self) -> dict:
        """Process one inbox scan cycle. Returns a summary dict."""
        messages = self._fetch_messages()
        log.info(f"InboxAgent: fetched {len(messages)} messages")

        drafted = 0
        filed = 0
        noise = 0

        for msg in messages:
            category = self._classify(msg)
            if category == "noise":
                self._file_noise(msg)
                noise += 1
            elif category in ("ticket", "partner", "internal", "actionable"):
                draft = self._draft_reply(msg)
                if draft:
                    self._save_draft(msg, draft)
                    self.bus.publish(
                        source="inbox_agent",
                        event_type="discovery",
                        topic="email_draft_ready",
                        subject=f"Draft ready: {msg.subject}",
                        payload={"msg_id": msg.msg_id, "sender": msg.sender,
                                 "category": category},
                        priority="normal",
                    )
                    drafted += 1
                else:
                    filed += 1
            else:
                filed += 1

        summary = {"drafted": drafted, "filed": filed, "noise": noise,
                   "total": len(messages)}
        log.info(f"InboxAgent cycle: {summary}")
        return summary

    # ── Classification ────────────────────────────────────────────────────

    def _classify(self, msg: EmailMessage) -> str:
        """Return category: noise | ticket | partner | internal | actionable | unknown."""
        sender_local = msg.sender.split("@")[0].lower() if "@" in msg.sender else msg.sender.lower()
        sender_domain = msg.sender.split("@")[-1].lower() if "@" in msg.sender else ""

        # Hard-noise check first
        if _NOISE_SENDER_PATTERNS.search(sender_local):
            return "noise"
        if _NOISE_SUBJECT_KEYWORDS.search(msg.subject):
            return "noise"

        # Ticket emails
        if _TICKET_SUBJECT_PATTERN.search(msg.subject):
            return "ticket"

        # Known partner domain
        if any(sender_domain.endswith(d) for d in self._known_partner_domains):
            return "partner"

        # Internal
        if sender_domain == self._company_domain:
            return "internal"

        # Default: treat as potentially actionable
        return "actionable"

    # ── Reply drafting ────────────────────────────────────────────────────

    @llm_retry(max_retries=2, base_delay=3.0)
    def _draft_reply(self, msg: EmailMessage) -> Optional[str]:
        """Call LLM to generate a context-aware reply draft."""
        if not config.llm_configured:
            log.debug("LLM not configured — returning placeholder draft")
            return (
                f"Thank you for reaching out regarding: {msg.subject}\n\n"
                "We have received your message and will respond shortly.\n\n"
                f"Best regards,\n{config.COMPANY_NAME} Team"
            )
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
            prompt = (
                f"You are a professional customer service representative for {config.COMPANY_NAME}.\n"
                f"Draft a helpful, concise reply to this email.\n\n"
                f"FROM: {msg.sender}\n"
                f"SUBJECT: {msg.subject}\n"
                f"BODY:\n{msg.body[:2000]}\n\n"
                f"Write ONLY the reply body — no subject line, no 'Draft:' prefix."
            )
            response = client.messages.create(
                model=config.DEFAULT_MODEL,
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text.strip()
        except Exception as exc:
            log.error(f"LLM draft failed: {exc}")
            return None

    # ── Storage / routing ─────────────────────────────────────────────────

    def _file_noise(self, msg: EmailMessage) -> None:
        """Route noise messages to a noise/automated folder (best-effort)."""
        log.debug(f"Noise: {msg}")

    def _save_draft(self, msg: EmailMessage, draft: str) -> None:
        """Persist draft reply (stub — override with real email provider call)."""
        log.info(f"Draft saved for {msg.msg_id}: {msg.subject[:60]}")

    # ── Message fetching ──────────────────────────────────────────────────

    def _fetch_messages(self) -> List[EmailMessage]:
        """
        Fetch messages from the configured provider.

        Override this method to integrate a real inbox source:
          - IMAP: use imaplib or imap_tools
          - Outlook (Windows): use win32com.client
          - Gmail: use google-auth + googleapiclient
        """
        # Demo / dry-run mode — return synthetic messages
        if not os.getenv("IMAP_HOST"):
            return _demo_messages()

        # IMAP implementation (example)
        return self._fetch_via_imap()

    def _fetch_via_imap(self) -> List[EmailMessage]:
        """Fetch unread messages via IMAP."""
        import email
        import imaplib
        host = os.getenv("IMAP_HOST", "")
        port = int(os.getenv("IMAP_PORT", "993"))
        user = os.getenv("IMAP_USER", "")
        password = os.getenv("IMAP_PASSWORD", "")
        messages = []
        try:
            conn = imaplib.IMAP4_SSL(host, port)
            conn.login(user, password)
            conn.select("INBOX")
            _, data = conn.search(None, "UNSEEN")
            msg_ids = (data[0].decode().split() if data[0] else [])[:config.MAX_EMAILS_PER_CYCLE]
            for mid in msg_ids:
                _, raw = conn.fetch(mid, "(RFC822)")
                parsed = email.message_from_bytes(raw[0][1])
                body = ""
                if parsed.is_multipart():
                    for part in parsed.walk():
                        if part.get_content_type() == "text/plain":
                            body = part.get_payload(decode=True).decode("utf-8", errors="replace")
                            break
                else:
                    body = parsed.get_payload(decode=True).decode("utf-8", errors="replace")
                messages.append(EmailMessage(
                    msg_id=mid.decode(),
                    subject=parsed.get("Subject", ""),
                    sender=parsed.get("From", ""),
                    body=body,
                    received_at=parsed.get("Date", ""),
                ))
            conn.logout()
        except Exception as exc:
            log.error(f"IMAP fetch failed: {exc}")
        return messages


import os


def _demo_messages() -> List[EmailMessage]:
    """Return synthetic demo messages for dry-run mode."""
    return [
        EmailMessage(
            msg_id="demo-001",
            subject="PO #BB-20240115-001 — Please Confirm Receipt",
            sender="edi@bigboxretail.example.com",
            body=(
                "Hi ACME team,\n\nCould you confirm receipt of our purchase order "
                "BB-20240115-001 for 500 units of SKU FOAM-QN-10?  We need an 855 "
                "acknowledgment by end of business today.\n\nThanks,\nBig Box Retail EDI Team"
            ),
        ),
        EmailMessage(
            msg_id="demo-002",
            subject="[Ticket #4521] Shipment delay inquiry",
            sender="support@helpdesk.example.com",
            body=(
                "A customer has opened a ticket regarding shipment delay for order #ORD-9988. "
                "Please advise on the expected ship date."
            ),
        ),
        EmailMessage(
            msg_id="demo-003",
            subject="Your monthly newsletter is here!",
            sender="newsletter@marketing.example.com",
            body="Check out our latest deals...",
        ),
    ]
