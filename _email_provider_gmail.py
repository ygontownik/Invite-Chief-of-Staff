"""
_email_provider_gmail.py — Gmail implementation of EmailProvider.

Uses the Gmail REST API via google-api-python-client. OAuth tokens are cached
to ~/credentials/gmail_mini_token.pickle. Scopes:
  - gmail.readonly  (search, get_thread, get_message)
  - gmail.compose   (create_draft, list_drafts)
"""
from __future__ import annotations

import base64
import email.utils
import pickle
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from typing import Optional

from _email_provider import (
    DraftHandle,
    EmailAddress,
    EmailAuthError,
    EmailMessage,
    EmailNotFoundError,
    EmailProvider,
    EmailProviderError,
    EmailThread,
)


_DEFAULT_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]


class GmailProvider(EmailProvider):
    """Gmail implementation. Uses pickled OAuth credentials."""

    def __init__(
        self,
        credentials_path: Optional[Path] = None,
        token_path: Optional[Path] = None,
        scopes: Optional[list[str]] = None,
        user_email: str = "me",
    ):
        self._creds_path = Path(credentials_path) if credentials_path else \
            Path.home() / "credentials" / "gdrive_credentials.json"
        self._token_path = Path(token_path) if token_path else \
            Path.home() / "credentials" / "gmail_mini_token.pickle"
        self._scopes = scopes or _DEFAULT_SCOPES
        self._user = user_email
        self._service = None  # lazily built

    @property
    def name(self) -> str:
        return "gmail"

    # ── Auth ────────────────────────────────────────────────────────────────

    def authorize(self) -> None:
        """Load cached token; refresh or run OAuth flow if needed."""
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
            from googleapiclient.discovery import build
        except ImportError as e:
            raise EmailProviderError(
                f"Missing dependency: {e}. Run: pip install google-auth "
                "google-auth-oauthlib google-api-python-client"
            )

        creds: Optional[Credentials] = None
        if self._token_path.exists():
            with open(self._token_path, "rb") as f:
                creds = pickle.load(f)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception as e:
                    raise EmailAuthError(f"Token refresh failed: {e}")
            else:
                if not self._creds_path.exists():
                    raise EmailAuthError(
                        f"No OAuth client at {self._creds_path}. Download "
                        "from Google Cloud Console (OAuth 2.0 Client ID, "
                        "Desktop app type) and save it there."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(self._creds_path), self._scopes
                )
                creds = flow.run_local_server(port=0, open_browser=True)
            self._token_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._token_path, "wb") as f:
                pickle.dump(creds, f)

        self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    def _svc(self):
        if self._service is None:
            self.authorize()
        return self._service

    # ── Search ──────────────────────────────────────────────────────────────

    def search_inbox(
        self,
        since: Optional[datetime] = None,
        query: Optional[str] = None,
        max_results: int = 50,
        include_sent: bool = False,
    ) -> list[EmailMessage]:
        # Build Gmail search query
        parts = []
        if query:
            parts.append(query)
        if since:
            ts = int(since.replace(tzinfo=since.tzinfo or timezone.utc).timestamp())
            parts.append(f"after:{ts}")
        if not include_sent:
            parts.append("-in:sent")

        # Default exclusions if no custom query
        if not query:
            parts.extend([
                "-from:noreply", "-from:no-reply",
                "-category:promotions", "-category:updates",
            ])

        q = " ".join(parts) if parts else None

        try:
            resp = self._svc().users().messages().list(
                userId=self._user,
                q=q,
                maxResults=max_results,
            ).execute()
        except Exception as e:
            raise EmailProviderError(f"Gmail search failed: {e}")

        msg_ids = [m["id"] for m in resp.get("messages", [])]

        # Fetch metadata for each (parallel-friendly via batch could be added later)
        results = []
        for mid in msg_ids:
            try:
                m = self._svc().users().messages().get(
                    userId=self._user, id=mid, format="metadata",
                    metadataHeaders=["From", "To", "Cc", "Subject", "Date"],
                ).execute()
                results.append(self._parse_message(m, with_body=False))
            except Exception:
                continue
        return results

    def search_sent(
        self,
        since: Optional[datetime] = None,
        max_results: int = 20,
    ) -> list[EmailMessage]:
        parts = ["in:sent"]
        if since:
            ts = int(since.replace(tzinfo=since.tzinfo or timezone.utc).timestamp())
            parts.append(f"after:{ts}")
        q = " ".join(parts)
        try:
            resp = self._svc().users().messages().list(
                userId=self._user,
                q=q,
                maxResults=max_results,
            ).execute()
        except Exception as e:
            raise EmailProviderError(f"Gmail sent search failed: {e}")

        results = []
        for mid in [m["id"] for m in resp.get("messages", [])]:
            try:
                m = self._svc().users().messages().get(
                    userId=self._user, id=mid, format="metadata",
                    metadataHeaders=["From", "To", "Cc", "Subject", "Date"],
                ).execute()
                msg = self._parse_message(m, with_body=False)
                msg.direction = "sent"
                results.append(msg)
            except Exception:
                continue
        return results

    # ── Threads & messages ──────────────────────────────────────────────────

    def get_thread(self, thread_id: str) -> EmailThread:
        try:
            t = self._svc().users().threads().get(
                userId=self._user, id=thread_id, format="full"
            ).execute()
        except Exception as e:
            if "Not Found" in str(e) or "404" in str(e):
                raise EmailNotFoundError(f"Thread {thread_id} not found")
            raise EmailProviderError(f"Gmail get_thread failed: {e}")

        messages = [self._parse_message(m, with_body=True) for m in t.get("messages", [])]
        subj = ""
        if messages:
            subj = messages[0].subject
        return EmailThread(id=thread_id, subject=subj, messages=messages, raw=t)

    def get_message(self, message_id: str) -> EmailMessage:
        try:
            m = self._svc().users().messages().get(
                userId=self._user, id=message_id, format="full"
            ).execute()
        except Exception as e:
            if "Not Found" in str(e) or "404" in str(e):
                raise EmailNotFoundError(f"Message {message_id} not found")
            raise EmailProviderError(f"Gmail get_message failed: {e}")
        return self._parse_message(m, with_body=True)

    # ── Drafts ──────────────────────────────────────────────────────────────

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
        # Build MIME message
        if body_html:
            msg = MIMEMultipart("alternative")
            msg.attach(MIMEText(body_text, "plain"))
            msg.attach(MIMEText(body_html, "html"))
        else:
            msg = MIMEText(body_text, "plain")

        msg["To"] = ", ".join(to)
        msg["Subject"] = subject
        if cc:
            msg["Cc"] = ", ".join(cc)
        if bcc:
            msg["Bcc"] = ", ".join(bcc)

        # Reply chaining via In-Reply-To / References headers
        if in_reply_to_message_id:
            try:
                orig = self._svc().users().messages().get(
                    userId=self._user, id=in_reply_to_message_id, format="metadata",
                    metadataHeaders=["Message-ID", "References"],
                ).execute()
                headers = {h["name"]: h["value"] for h in orig.get("payload", {}).get("headers", [])}
                rfc_id = headers.get("Message-ID") or headers.get("Message-Id")
                if rfc_id:
                    msg["In-Reply-To"] = rfc_id
                    msg["References"] = (headers.get("References", "") + " " + rfc_id).strip()
            except Exception:
                pass

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        body = {"message": {"raw": raw}}
        if in_reply_to_thread_id:
            body["message"]["threadId"] = in_reply_to_thread_id

        try:
            resp = self._svc().users().drafts().create(
                userId=self._user, body=body
            ).execute()
        except Exception as e:
            raise EmailProviderError(f"Gmail create_draft failed: {e}")

        draft_id = resp.get("id", "")
        return DraftHandle(
            id=draft_id,
            web_url=self.drafts_url(),
            raw=resp,
        )

    def list_drafts(self, max_results: int = 50) -> list[DraftHandle]:
        try:
            resp = self._svc().users().drafts().list(
                userId=self._user, maxResults=max_results
            ).execute()
        except Exception as e:
            raise EmailProviderError(f"Gmail list_drafts failed: {e}")
        return [
            DraftHandle(id=d.get("id", ""), web_url=self.drafts_url(), raw=d)
            for d in resp.get("drafts", [])
        ]

    # ── Internal: Gmail message parser ──────────────────────────────────────

    def _parse_message(self, m: dict, with_body: bool) -> EmailMessage:
        payload = m.get("payload", {})
        headers = {h["name"]: h["value"] for h in payload.get("headers", [])}

        sender = self._parse_address(headers.get("From", ""))
        recipients = self._parse_address_list(headers.get("To", ""))
        cc = self._parse_address_list(headers.get("Cc", ""))

        received_at = None
        date_str = headers.get("Date", "")
        if date_str:
            try:
                received_at = email.utils.parsedate_to_datetime(date_str)
                if received_at and received_at.tzinfo is None:
                    received_at = received_at.replace(tzinfo=timezone.utc)
            except Exception:
                pass

        body_text = body_html = ""
        if with_body:
            body_text, body_html = self._extract_body(payload)

        has_attachments = self._has_attachments(payload)
        labels = m.get("labelIds", []) or []
        is_unread = "UNREAD" in labels
        direction = "sent" if "SENT" in labels else "received"

        return EmailMessage(
            id=m.get("id", ""),
            thread_id=m.get("threadId", ""),
            subject=headers.get("Subject", ""),
            sender=sender,
            recipients=recipients,
            cc=cc,
            snippet=m.get("snippet", ""),
            body_text=body_text,
            body_html=body_html,
            received_at=received_at,
            has_attachments=has_attachments,
            labels=labels,
            is_unread=is_unread,
            direction=direction,
            raw=m,
        )

    @staticmethod
    def _parse_address(s: str) -> EmailAddress:
        if not s:
            return EmailAddress()
        name, email_addr = email.utils.parseaddr(s)
        return EmailAddress(name=name.strip(), email=email_addr.strip().lower())

    @classmethod
    def _parse_address_list(cls, s: str) -> list[EmailAddress]:
        if not s:
            return []
        parts = email.utils.getaddresses([s])
        return [EmailAddress(name=n.strip(), email=e.strip().lower()) for n, e in parts if e]

    @classmethod
    def _extract_body(cls, payload: dict) -> tuple[str, str]:
        """Walk MIME parts; return (plain, html) bodies."""
        plain = ""
        html = ""

        def walk(part: dict):
            nonlocal plain, html
            mime = part.get("mimeType", "")
            body = part.get("body", {})
            data = body.get("data")
            if data and mime == "text/plain" and not plain:
                try:
                    plain = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                except Exception:
                    pass
            elif data and mime == "text/html" and not html:
                try:
                    html = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                except Exception:
                    pass
            for sub in part.get("parts", []) or []:
                walk(sub)

        walk(payload)
        return plain, html

    @classmethod
    def _has_attachments(cls, payload: dict) -> bool:
        def walk(part: dict) -> bool:
            if part.get("filename"):
                return True
            for sub in part.get("parts", []) or []:
                if walk(sub):
                    return True
            return False
        return walk(payload)
