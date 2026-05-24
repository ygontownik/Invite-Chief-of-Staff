"""OAuth credential loader — reuses ~/credentials/token.json."""
import os
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

_TOKEN_FILE = os.path.expanduser("~/credentials/token.json")
_SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
]


def get_credentials() -> Credentials:
    creds = Credentials.from_authorized_user_file(_TOKEN_FILE, _SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(_TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return creds
