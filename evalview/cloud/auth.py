"""Cloud auth management — stores session in ~/.evalview/auth.json."""

import json
import os
import stat
from pathlib import Path
from typing import Optional, Dict, Any


AUTH_FILE = Path.home() / ".evalview" / "auth.json"


class CloudAuth:
    """Manages cloud authentication state in ~/.evalview/auth.json."""

    def save(
        self,
        access_token: str,
        refresh_token: str,
        user_id: str,
        email: str,
    ) -> None:
        """Persist auth session to disk (chmod 600)."""
        AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "user_id": user_id,
            "email": email,
        }
        AUTH_FILE.write_text(json.dumps(data, indent=2))
        # Restrict to owner read/write only
        AUTH_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)

    def load(self) -> Optional[Dict[str, Any]]:
        """Return parsed auth data, or None if missing/malformed."""
        if not AUTH_FILE.exists():
            return None
        try:
            data = json.loads(AUTH_FILE.read_text())
            # Validate required fields
            if all(k in data for k in ("access_token", "refresh_token", "user_id", "email")):
                return data
        except Exception:
            pass
        return None

    def clear(self) -> None:
        """Delete the auth file (logout)."""
        if AUTH_FILE.exists():
            AUTH_FILE.unlink()

    def is_logged_in(self) -> bool:
        """Return True if a valid session exists on disk."""
        return self.load() is not None

    def get_access_token(self) -> Optional[str]:
        data = self.load()
        return data["access_token"] if data else None

    def get_refresh_token(self) -> Optional[str]:
        data = self.load()
        return data["refresh_token"] if data else None

    def get_user_id(self) -> Optional[str]:
        data = self.load()
        return data["user_id"] if data else None

    def get_email(self) -> Optional[str]:
        data = self.load()
        return data["email"] if data else None

    def update_tokens(self, access_token: str, refresh_token: str) -> None:
        """Update tokens in place (used after a token refresh)."""
        data = self.load()
        if data is None:
            return
        data["access_token"] = access_token
        data["refresh_token"] = refresh_token
        AUTH_FILE.write_text(json.dumps(data, indent=2))
        AUTH_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)
