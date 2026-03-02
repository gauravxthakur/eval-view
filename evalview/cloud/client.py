"""Thin httpx wrapper around the Supabase Storage REST API."""

import json
from typing import Any, Dict, List, Optional

import httpx

# ---------------------------------------------------------------------------
# Supabase project constants — both are PUBLIC values safe to ship in OSS.
# RLS policies enforce that users can only access their own folder.
# ---------------------------------------------------------------------------
SUPABASE_URL = "https://placeholder.supabase.co"   # Replace with real project URL
SUPABASE_ANON_KEY = "placeholder-anon-key"          # Replace with real anon key

BUCKET = "goldens"
_STORAGE = f"{SUPABASE_URL}/storage/v1"
_AUTH = f"{SUPABASE_URL}/auth/v1"


class CloudSyncError(Exception):
    """Raised when a cloud operation fails. Always caught in the CLI."""


class CloudClient:
    """Async client for EvalView Cloud (Supabase Storage)."""

    def __init__(self, access_token: str) -> None:
        self._token = access_token
        self._headers = {
            "Authorization": f"Bearer {access_token}",
            "apikey": SUPABASE_ANON_KEY,
        }

    # ------------------------------------------------------------------
    # Storage operations
    # ------------------------------------------------------------------

    async def upload_golden(
        self, user_id: str, test_name: str, data: Dict[str, Any]
    ) -> None:
        """Upload a golden baseline JSON to cloud storage."""
        path = f"{user_id}/{test_name}.golden.json"
        url = f"{_STORAGE}/object/{BUCKET}/{path}"
        body = json.dumps(data).encode()
        headers = {
            **self._headers,
            "Content-Type": "application/json",
            "x-upsert": "true",
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.put(url, content=body, headers=headers)
                if resp.status_code == 401:
                    raise CloudSyncError("Unauthorized — token may be expired")
                resp.raise_for_status()
        except CloudSyncError:
            raise
        except Exception as exc:
            raise CloudSyncError(f"Upload failed for {test_name}: {exc}") from exc

    async def download_golden(
        self, user_id: str, test_name: str
    ) -> Optional[Dict[str, Any]]:
        """Download a golden baseline from cloud storage. Returns None if not found."""
        path = f"{user_id}/{test_name}.golden.json"
        url = f"{_STORAGE}/object/{BUCKET}/{path}"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url, headers=self._headers)
                if resp.status_code == 404:
                    return None
                if resp.status_code == 401:
                    raise CloudSyncError("Unauthorized — token may be expired")
                resp.raise_for_status()
                return resp.json()
        except CloudSyncError:
            raise
        except Exception as exc:
            raise CloudSyncError(f"Download failed for {test_name}: {exc}") from exc

    async def list_goldens(self, user_id: str) -> List[str]:
        """Return test names stored for this user (without .golden.json suffix)."""
        url = f"{_STORAGE}/object/list/{BUCKET}"
        body = {"prefix": f"{user_id}/", "limit": 1000, "offset": 0}
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(url, json=body, headers=self._headers)
                if resp.status_code == 401:
                    raise CloudSyncError("Unauthorized — token may be expired")
                resp.raise_for_status()
                items = resp.json()
                names = []
                for item in items:
                    name = item.get("name", "")
                    # Strip "{user_id}/" prefix and ".golden.json" suffix
                    prefix = f"{user_id}/"
                    if name.startswith(prefix):
                        name = name[len(prefix):]
                    if name.endswith(".golden.json"):
                        name = name[: -len(".golden.json")]
                    if name:
                        names.append(name)
                return names
        except CloudSyncError:
            raise
        except Exception as exc:
            raise CloudSyncError(f"List failed: {exc}") from exc

    async def delete_golden(self, user_id: str, test_name: str) -> None:
        """Delete a golden baseline from cloud storage."""
        path = f"{user_id}/{test_name}.golden.json"
        url = f"{_STORAGE}/object/{BUCKET}/{path}"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.delete(url, headers=self._headers)
                if resp.status_code == 401:
                    raise CloudSyncError("Unauthorized — token may be expired")
                resp.raise_for_status()
        except CloudSyncError:
            raise
        except Exception as exc:
            raise CloudSyncError(f"Delete failed for {test_name}: {exc}") from exc

    # ------------------------------------------------------------------
    # Token refresh
    # ------------------------------------------------------------------

    async def refresh_token(self, refresh_token: str) -> Optional[Dict[str, Any]]:
        """Attempt a token refresh. Returns new session dict or None on failure."""
        url = f"{_AUTH}/token?grant_type=refresh_token"
        body = {"refresh_token": refresh_token}
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    url,
                    json=body,
                    headers={"apikey": SUPABASE_ANON_KEY, "Content-Type": "application/json"},
                )
                if resp.status_code != 200:
                    return None
                return resp.json()
        except Exception:
            return None

    # ------------------------------------------------------------------
    # OAuth helpers (used by the login command)
    # ------------------------------------------------------------------

    @staticmethod
    def build_oauth_url(redirect_uri: str) -> str:
        """Build the GitHub OAuth authorization URL."""
        return (
            f"{_AUTH}/authorize"
            f"?provider=github"
            f"&redirect_to={redirect_uri}"
        )

    @staticmethod
    async def exchange_code(code: str) -> Optional[Dict[str, Any]]:
        """Exchange an OAuth code for a Supabase session. Returns None on failure."""
        url = f"{_AUTH}/token?grant_type=pkce"
        body = {"auth_code": code}
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    url,
                    json=body,
                    headers={"apikey": SUPABASE_ANON_KEY, "Content-Type": "application/json"},
                )
                if resp.status_code != 200:
                    return None
                return resp.json()
        except Exception:
            return None
