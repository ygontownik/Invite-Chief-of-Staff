"""
_email_provider_outlook.py — Microsoft 365 / Outlook implementation of EmailProvider.

Uses the Microsoft Graph API (https://graph.microsoft.com/v1.0). OAuth tokens
are cached to ~/credentials/ms_token.json. Required scopes:
  - Mail.Read         (search, get_thread/messages)
  - Mail.ReadWrite    (create_draft, list_drafts)
  - Mail.Send         (only if you ever auto-send — drafts only need ReadWrite)
  - User.Read         (basic profile, needed by Graph by default)
  - offline_access    (refresh tokens)

OAuth flow:
  1. Register an app at https://portal.azure.com → Azure Active Directory
     → App registrations → New registration. Type: "Public client" / native.
  2. Copy the Application (client) ID into ~/credentials/ms_oauth_client.json:
       {"client_id": "00000000-0000-0000-0000-000000000000",
        "tenant_id": "common",
        "redirect_uri": "http://localhost:8765/callback"}
  3. First run of any script using this provider will open the browser for
     consent, then cache the token at ~/credentials/ms_token.json.

Note: Microsoft Graph does NOT have a true "thread" concept like Gmail. We
emulate threads via conversationId — messages with the same conversationId
are sequential exchanges on the same topic.
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
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
    EmailRateLimitError,
    EmailThread,
)


GRAPH_BASE = "https://graph.microsoft.com/v1.0"
DEFAULT_SCOPES = [
    "Mail.Read",
    "Mail.ReadWrite",
    "User.Read",
    "offline_access",
]


class OutlookProvider(EmailProvider):
    """Microsoft 365 / Outlook implementation via Graph API."""

    def __init__(
        self,
        client_config_path: Optional[Path] = None,
        token_path: Optional[Path] = None,
        scopes: Optional[list[str]] = None,
    ):
        self._client_config_path = Path(client_config_path) if client_config_path else \
            Path.home() / "credentials" / "ms_oauth_client.json"
        self._token_path = Path(token_path) if token_path else \
            Path.home() / "credentials" / "ms_token.json"
        self._scopes = scopes or DEFAULT_SCOPES
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0

    @property
    def name(self) -> str:
        return "outlook"

    # ── Auth ────────────────────────────────────────────────────────────────

    def authorize(self) -> None:
        """Load cached token; refresh or run device-code OAuth flow if needed."""
        token_data = None
        if self._token_path.exists():
            try:
                with open(self._token_path) as f:
                    token_data = json.load(f)
            except Exception:
                token_data = None

        if token_data and token_data.get("expires_at", 0) > time.time() + 60:
            # Cached token still valid
            self._access_token = token_data.get("access_token")
            self._token_expires_at = token_data.get("expires_at", 0)
            return

        if token_data and token_data.get("refresh_token"):
            try:
                self._refresh(token_data)
                return
            except EmailAuthError:
                pass  # fall through to fresh device-code flow

        # Fresh OAuth flow via device-code grant (no redirect URI server needed)
        self._device_code_flow()

    def _refresh(self, token_data: dict) -> None:
        client_cfg = self._load_client_config()
        tenant = client_cfg.get("tenant_id", "common")
        url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
        data = urllib.parse.urlencode({
            "client_id": client_cfg["client_id"],
            "scope": " ".join(self._scopes),
            "refresh_token": token_data["refresh_token"],
            "grant_type": "refresh_token",
        }).encode()
        try:
            req = urllib.request.Request(url, data=data, method="POST")
            with urllib.request.urlopen(req, timeout=15) as r:
                body = json.loads(r.read())
        except Exception as e:
            raise EmailAuthError(f"MS token refresh failed: {e}")

        self._access_token = body["access_token"]
        expires_at = time.time() + body.get("expires_in", 3600)
        self._token_expires_at = expires_at
        # Persist updated token
        token_data.update({
            "access_token": body["access_token"],
            "expires_at": expires_at,
            "refresh_token": body.get("refresh_token", token_data.get("refresh_token")),
        })
        self._token_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._token_path, "w") as f:
            json.dump(token_data, f, indent=2)

    def _device_code_flow(self) -> None:
        """Microsoft device-code OAuth — prints a URL+code, user authorizes in browser."""
        client_cfg = self._load_client_config()
        tenant = client_cfg.get("tenant_id", "common")
        cid = client_cfg["client_id"]

        # Step 1: get device code
        url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/devicecode"
        data = urllib.parse.urlencode({
            "client_id": cid,
            "scope": " ".join(self._scopes),
        }).encode()
        try:
            req = urllib.request.Request(url, data=data, method="POST")
            with urllib.request.urlopen(req, timeout=15) as r:
                code_resp = json.loads(r.read())
        except Exception as e:
            raise EmailAuthError(f"Device code request failed: {e}")

        print()
        print("═" * 60)
        print("  Microsoft 365 / Outlook authorization needed")
        print("═" * 60)
        print(f"\n  1. Open this URL in your browser:")
        print(f"\n     {code_resp['verification_uri']}\n")
        print(f"  2. Enter this code:  {code_resp['user_code']}\n")
        print(f"  3. Sign in with the Microsoft account whose mailbox the")
        print(f"     pipeline should access (your Outlook work account).\n")
        print(f"  Waiting for you to complete authorization (timeout {code_resp['expires_in']}s)...")
        print("═" * 60)

        # Step 2: poll for completion
        token_url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
        interval = code_resp.get("interval", 5)
        deadline = time.time() + code_resp.get("expires_in", 900)

        while time.time() < deadline:
            time.sleep(interval)
            poll_data = urllib.parse.urlencode({
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": cid,
                "device_code": code_resp["device_code"],
            }).encode()
            try:
                req = urllib.request.Request(token_url, data=poll_data, method="POST")
                with urllib.request.urlopen(req, timeout=15) as r:
                    token_resp = json.loads(r.read())
            except urllib.error.HTTPError as e:
                err_body = e.read()
                try:
                    err_json = json.loads(err_body)
                    err_code = err_json.get("error", "")
                    if err_code in ("authorization_pending", "slow_down"):
                        if err_code == "slow_down":
                            interval += 5
                        continue
                    if err_code == "expired_token":
                        raise EmailAuthError("Device code expired. Re-run authorize().")
                    raise EmailAuthError(f"Device code error: {err_json}")
                except json.JSONDecodeError:
                    raise EmailAuthError(f"Device code poll failed: {err_body}")
            except Exception as e:
                raise EmailAuthError(f"Device code poll failed: {e}")

            # Success
            self._access_token = token_resp["access_token"]
            expires_at = time.time() + token_resp.get("expires_in", 3600)
            self._token_expires_at = expires_at
            self._token_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._token_path, "w") as f:
                json.dump({
                    "access_token": token_resp["access_token"],
                    "refresh_token": token_resp.get("refresh_token", ""),
                    "expires_at": expires_at,
                    "scopes": self._scopes,
                }, f, indent=2)
            print("  ✓ Authorized — token cached.\n")
            return

        raise EmailAuthError("Device code authorization timed out.")

    # Microsoft's Azure CLI public client — Microsoft pre-registered, no Azure
    # subscription / app registration required to use it. Fine for personal /
    # individual use (it's the same client ID `az login` uses). Enterprise users
    # should register their own app for audit and policy compliance.
    _PUBLIC_FALLBACK_CLIENT_ID = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"
    _PUBLIC_FALLBACK_TENANT = "common"

    def _load_client_config(self) -> dict:
        if self._client_config_path.exists():
            with open(self._client_config_path) as f:
                return json.load(f)
        # Fallback to public client — works for personal Outlook accounts
        # without requiring the user to register an app in Azure portal.
        # Print one-time notice so it's clear what's happening.
        if not getattr(self, "_warned_public_client", False):
            print(
                "\n  [outlook] Using Microsoft's public Azure-CLI client_id as fallback.\n"
                f"  [outlook] No client config found at {self._client_config_path}\n"
                "  [outlook] To use a private app registration instead, create that file with:\n"
                '  [outlook]   {"client_id": "<your-app-id>", "tenant_id": "consumers"}\n'
            )
            self._warned_public_client = True
        return {
            "client_id": self._PUBLIC_FALLBACK_CLIENT_ID,
            "tenant_id": self._PUBLIC_FALLBACK_TENANT,
        }

    # ── Graph helpers ───────────────────────────────────────────────────────

    def _ensure_token(self) -> str:
        if not self._access_token or time.time() > self._token_expires_at - 60:
            self.authorize()
        return self._access_token  # type: ignore[return-value]

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        url = f"{GRAPH_BASE}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params, safe="$,()'")
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self._ensure_token()}",
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            self._handle_http_error(e)
            raise

    def _post(self, path: str, body: dict) -> dict:
        url = f"{GRAPH_BASE}{path}"
        data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "Authorization": f"Bearer {self._ensure_token()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            self._handle_http_error(e)
            raise

    @staticmethod
    def _handle_http_error(e):
        try:
            err = json.loads(e.read())
            msg = err.get("error", {}).get("message", str(e))
            code = err.get("error", {}).get("code", "")
        except Exception:
            msg = str(e)
            code = ""
        if e.code == 401:
            raise EmailAuthError(f"Graph 401 ({code}): {msg}")
        if e.code == 404:
            raise EmailNotFoundError(f"Graph 404 ({code}): {msg}")
        if e.code == 429:
            raise EmailRateLimitError(f"Graph 429 ({code}): {msg}")
        raise EmailProviderError(f"Graph {e.code} ({code}): {msg}")

    # ── Search ──────────────────────────────────────────────────────────────

    def search_inbox(
        self,
        since: Optional[datetime] = None,
        query: Optional[str] = None,
        max_results: int = 50,
        include_sent: bool = False,
    ) -> list[EmailMessage]:
        # Graph filter syntax
        params = {
            "$top": str(max_results),
            "$orderby": "receivedDateTime desc",
            "$select": ("id,conversationId,subject,from,toRecipients,ccRecipients,"
                        "receivedDateTime,bodyPreview,hasAttachments,isRead"),
        }

        filters = []
        if since:
            ts = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            filters.append(f"receivedDateTime ge {ts}")
        if query:
            # Graph supports $search OR $filter, not both. Prefer $search if query is freeform.
            params["$search"] = f'"{query}"'
            params.pop("$orderby", None)  # Graph rejects orderby with search
        if filters and "$search" not in params:
            params["$filter"] = " and ".join(filters)

        path = "/me/messages" if include_sent else "/me/mailFolders/inbox/messages"
        try:
            resp = self._get(path, params)
        except EmailProviderError:
            raise

        return [self._parse_message(m, with_body=False) for m in resp.get("value", [])]

    def search_sent(
        self,
        since: Optional[datetime] = None,
        max_results: int = 20,
    ) -> list[EmailMessage]:
        params = {
            "$top": str(max_results),
            "$orderby": "sentDateTime desc",
            "$select": ("id,conversationId,subject,from,toRecipients,ccRecipients,"
                        "sentDateTime,bodyPreview,hasAttachments,isRead"),
        }
        if since:
            ts = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            params["$filter"] = f"sentDateTime ge {ts}"

        try:
            resp = self._get("/me/mailFolders/sentItems/messages", params)
        except EmailProviderError:
            raise

        results = []
        for m in resp.get("value", []):
            msg = self._parse_message(m, with_body=False)
            msg.direction = "sent"
            # sentDateTime may be under a different key
            if not msg.received_at and m.get("sentDateTime"):
                try:
                    from datetime import datetime as _dt
                    msg.received_at = _dt.fromisoformat(
                        m["sentDateTime"].replace("Z", "+00:00")
                    )
                except Exception:
                    pass
            results.append(msg)
        return results

    # ── Threads & messages ──────────────────────────────────────────────────

    def get_thread(self, thread_id: str) -> EmailThread:
        """Outlook 'threads' map to conversationId — fetch all messages with that ID."""
        params = {
            "$filter": f"conversationId eq '{thread_id}'",
            "$orderby": "receivedDateTime asc",
            "$top": "100",
        }
        try:
            resp = self._get("/me/messages", params)
        except EmailNotFoundError:
            raise
        msgs = [self._parse_message(m, with_body=True) for m in resp.get("value", [])]
        if not msgs:
            raise EmailNotFoundError(f"No messages with conversationId={thread_id}")
        return EmailThread(id=thread_id, subject=msgs[0].subject, messages=msgs, raw=resp)

    def get_message(self, message_id: str) -> EmailMessage:
        try:
            m = self._get(f"/me/messages/{message_id}")
        except EmailNotFoundError:
            raise
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
        # Graph has a dedicated reply endpoint that preserves threading correctly
        if in_reply_to_message_id:
            try:
                # createReply returns a draft message we then update with our content
                draft = self._post(
                    f"/me/messages/{in_reply_to_message_id}/createReply",
                    {"comment": ""},
                )
                draft_id = draft["id"]
                # Patch in our actual content + recipients
                self._post(f"/me/messages/{draft_id}/update", {
                    "subject": subject,
                    "body": {
                        "contentType": "HTML" if body_html else "Text",
                        "content": body_html or body_text,
                    },
                    "toRecipients": [{"emailAddress": {"address": e}} for e in to],
                    "ccRecipients": [{"emailAddress": {"address": e}} for e in (cc or [])],
                    "bccRecipients": [{"emailAddress": {"address": e}} for e in (bcc or [])],
                })
                web_url = draft.get("webLink", self.drafts_url())
                return DraftHandle(id=draft_id, web_url=web_url, raw=draft)
            except Exception:
                pass  # fall through to standalone draft

        # Standalone draft (not a reply)
        body = {
            "subject": subject,
            "body": {
                "contentType": "HTML" if body_html else "Text",
                "content": body_html or body_text,
            },
            "toRecipients": [{"emailAddress": {"address": e}} for e in to],
            "ccRecipients": [{"emailAddress": {"address": e}} for e in (cc or [])],
            "bccRecipients": [{"emailAddress": {"address": e}} for e in (bcc or [])],
        }
        try:
            resp = self._post("/me/messages", body)
        except EmailProviderError:
            raise
        return DraftHandle(
            id=resp.get("id", ""),
            web_url=resp.get("webLink", self.drafts_url()),
            raw=resp,
        )

    def list_drafts(self, max_results: int = 50) -> list[DraftHandle]:
        params = {
            "$top": str(max_results),
            "$orderby": "lastModifiedDateTime desc",
            "$select": "id,webLink,subject",
        }
        try:
            resp = self._get("/me/mailFolders/drafts/messages", params)
        except EmailProviderError:
            raise
        return [
            DraftHandle(id=d.get("id", ""), web_url=d.get("webLink", ""), raw=d)
            for d in resp.get("value", [])
        ]

    # ── Internal: Graph message parser ──────────────────────────────────────

    def _parse_message(self, m: dict, with_body: bool) -> EmailMessage:
        sender_obj = (m.get("from") or {}).get("emailAddress") or {}
        sender = EmailAddress(
            name=sender_obj.get("name", ""),
            email=(sender_obj.get("address") or "").lower(),
        )

        def parse_addrs(field):
            return [
                EmailAddress(
                    name=(x.get("emailAddress") or {}).get("name", ""),
                    email=((x.get("emailAddress") or {}).get("address") or "").lower(),
                )
                for x in (m.get(field) or [])
            ]

        recipients = parse_addrs("toRecipients")
        cc = parse_addrs("ccRecipients")

        received_at = None
        rcv = m.get("receivedDateTime")
        if rcv:
            try:
                received_at = datetime.strptime(rcv, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except ValueError:
                # Some responses include fractional seconds
                try:
                    received_at = datetime.fromisoformat(rcv.replace("Z", "+00:00"))
                except Exception:
                    pass

        body_text = body_html = ""
        if with_body:
            body = m.get("body") or {}
            content = body.get("content", "")
            if body.get("contentType", "").lower() == "html":
                body_html = content
            else:
                body_text = content

        labels = []
        if m.get("isRead") is False:
            labels.append("UNREAD")

        return EmailMessage(
            id=m.get("id", ""),
            thread_id=m.get("conversationId", ""),
            subject=m.get("subject", ""),
            sender=sender,
            recipients=recipients,
            cc=cc,
            snippet=m.get("bodyPreview", ""),
            body_text=body_text,
            body_html=body_html,
            received_at=received_at,
            has_attachments=bool(m.get("hasAttachments")),
            labels=labels,
            is_unread=(not m.get("isRead", True)),
            raw=m,
        )
