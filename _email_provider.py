"""
_email_provider.py — Abstract email provider interface.

The COS pipeline talks to inbox/threads/drafts through this interface so the
same scripts (cos_email_mini.py, cos_capture_pipeline.py) work against Gmail
or Microsoft 365 (Outlook) without modification.

Selection happens at runtime via firm_config.json["email_provider"]:

    "email_provider": "gmail"     → _email_provider_gmail.GmailProvider
    "email_provider": "outlook"   → _email_provider_outlook.OutlookProvider

Adding a new provider (e.g., FastMail, ProtonMail Bridge) means implementing
the abstract class below and registering it in `get_email_provider()`.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ── Data classes (provider-agnostic) ─────────────────────────────────────────

@dataclass
class EmailAddress:
    name: str = ""
    email: str = ""

    def __str__(self) -> str:
        if self.name and self.email:
            return f"{self.name} <{self.email}>"
        return self.email or self.name


@dataclass
class EmailMessage:
    """A single email message — provider-normalised."""
    id: str
    thread_id: str
    subject: str
    sender: EmailAddress
    recipients: list[EmailAddress] = field(default_factory=list)
    cc: list[EmailAddress] = field(default_factory=list)
    snippet: str = ""
    body_text: str = ""
    body_html: str = ""
    received_at: Optional[datetime] = None
    has_attachments: bool = False
    labels: list[str] = field(default_factory=list)
    is_unread: bool = False
    raw: dict = field(default_factory=dict)


@dataclass
class EmailThread:
    """A conversation thread (sequence of messages on the same topic)."""
    id: str
    subject: str
    messages: list[EmailMessage] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


@dataclass
class DraftHandle:
    """Reference to a created draft so callers can build click-through URLs."""
    id: str
    web_url: str = ""   # provider-specific URL the dashboard can deep-link to
    raw: dict = field(default_factory=dict)


# ── Abstract base ─────────────────────────────────────────────────────────────

class EmailProvider(ABC):
    """
    Provider-agnostic email interface. Implementations live in
    _email_provider_gmail.py and _email_provider_outlook.py.

    Implementations MUST:
      - Authenticate at construction or via authorize() with cached tokens.
      - Raise the standard exceptions defined below on auth/network errors.
      - Normalize provider responses into the dataclasses above.
      - Be safe to construct multiple times (no global mutable state).
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier: 'gmail' or 'outlook'."""

    @abstractmethod
    def authorize(self) -> None:
        """Load cached tokens or trigger OAuth flow. Idempotent."""

    @abstractmethod
    def search_inbox(
        self,
        since: Optional[datetime] = None,
        query: Optional[str] = None,
        max_results: int = 50,
        include_sent: bool = False,
    ) -> list[EmailMessage]:
        """
        Search the user's inbox.

        Args:
            since:       Only return messages received after this UTC datetime.
            query:       Provider-specific filter string. Implementations should
                         interpret common patterns ("from:foo", "has:attachment")
                         in their native query syntax.
            max_results: Cap on returned messages (paginate internally if needed).
            include_sent: If True, also include messages from Sent folder.

        Returns:
            List of EmailMessage, ordered newest-first.
        """

    @abstractmethod
    def get_thread(self, thread_id: str) -> EmailThread:
        """Fetch a full thread with all messages and bodies."""

    @abstractmethod
    def get_message(self, message_id: str) -> EmailMessage:
        """Fetch a single message with full body content."""

    @abstractmethod
    def create_draft(
        self,
        to: list[str],
        subject: str,
        body_text: str,
        body_html: Optional[str] = None,
        cc: Optional[list[str]] = None,
        bcc: Optional[list[str]] = None,
        in_reply_to_message_id: Optional[str] = None,
        in_reply_to_thread_id: Optional[str] = None,
    ) -> DraftHandle:
        """
        Create an unsent draft in the user's account.

        Args:
            to: List of recipient email addresses.
            subject: Subject line. For replies, implementations should not
                     prepend "Re:" automatically (caller controls that).
            body_text: Plain-text body.
            body_html: Optional HTML body.
            cc/bcc: Optional cc/bcc recipients.
            in_reply_to_message_id: Provider message ID to chain into.
            in_reply_to_thread_id: Provider thread ID to attach the draft to.

        Returns:
            DraftHandle with provider-specific id and web URL.
        """

    @abstractmethod
    def list_drafts(self, max_results: int = 50) -> list[DraftHandle]:
        """List unsent drafts in the user's account."""

    # ── Optional helpers (default implementations) ──────────────────────────

    def drafts_url(self) -> str:
        """Browser URL for the user's drafts folder. Default to common patterns."""
        if self.name == "gmail":
            return "https://mail.google.com/mail/u/0/#drafts"
        if self.name == "outlook":
            return "https://outlook.office.com/mail/drafts"
        return ""

    def thread_url(self, thread_id: str) -> str:
        """Browser URL for a specific thread. Implementations can override."""
        if self.name == "gmail":
            return f"https://mail.google.com/mail/u/0/#all/{thread_id}"
        if self.name == "outlook":
            return f"https://outlook.office.com/mail/inbox/id/{thread_id}"
        return ""


# ── Standard exceptions ───────────────────────────────────────────────────────

class EmailProviderError(Exception):
    """Base exception — wrap any provider-side failure."""


class EmailAuthError(EmailProviderError):
    """OAuth/token failure. Caller should refresh credentials and retry."""


class EmailNotFoundError(EmailProviderError):
    """Requested message/thread/draft does not exist."""


class EmailRateLimitError(EmailProviderError):
    """Provider rate-limited the request. Retry with backoff."""


# ── Factory ───────────────────────────────────────────────────────────────────

def get_email_provider(provider_name: str, **kwargs) -> EmailProvider:
    """
    Instantiate the provider implementation matching firm_config["email_provider"].

    Args:
        provider_name: "gmail" | "outlook" (case-insensitive).
        **kwargs: Forwarded to the provider's constructor (e.g., credentials_path).

    Returns:
        A constructed (but not yet authorized) EmailProvider instance.

    Usage:
        provider = get_email_provider(firm_cfg["email_provider"])
        provider.authorize()
        msgs = provider.search_inbox(since=datetime.now(timezone.utc) - timedelta(hours=2))
    """
    name = (provider_name or "gmail").lower().strip()

    if name == "gmail":
        from _email_provider_gmail import GmailProvider
        return GmailProvider(**kwargs)
    if name in ("outlook", "office365", "microsoft365", "ms365", "msgraph"):
        from _email_provider_outlook import OutlookProvider
        return OutlookProvider(**kwargs)

    raise EmailProviderError(
        f"Unknown email_provider '{provider_name}'. "
        f"Supported: 'gmail', 'outlook'. Set firm_config.json[\"email_provider\"]."
    )


__all__ = [
    "EmailProvider",
    "EmailMessage",
    "EmailThread",
    "EmailAddress",
    "DraftHandle",
    "EmailProviderError",
    "EmailAuthError",
    "EmailNotFoundError",
    "EmailRateLimitError",
    "get_email_provider",
]
