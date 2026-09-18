"""
Seafile Vault Client for Voice Notes Pipeline.
Provides atomic upload and update of Markdown notes to Seafile repository via REST API.
"""

import os
import logging
import urllib.parse
from typing import Optional, Union
import requests

logger = logging.getLogger(__name__)

DEFAULT_SEAFILE_URL = "https://seafile.eyenology.net"
DEFAULT_SEAFILE_BASE_PATH = "/VoiceNotes/Inbox"


class SeafileVaultClient:
    """Client for synchronizing notes to a remote Seafile Obsidian vault via REST API."""

    def __init__(
        self,
        server_url: Optional[str] = None,
        token: Optional[str] = None,
        repo_id: Optional[str] = None,
        base_path: Optional[str] = None,
    ):
        if server_url is not None:
            self.server_url = server_url.rstrip("/")
        else:
            self.server_url = os.environ.get("SEAFILE_URL", DEFAULT_SEAFILE_URL).rstrip("/")

        self.token = token if token is not None else os.environ.get("SEAFILE_TOKEN")
        self.repo_id = repo_id if repo_id is not None else os.environ.get("SEAFILE_REPO_ID")

        raw_base = base_path if base_path is not None else os.environ.get("SEAFILE_BASE_PATH", DEFAULT_SEAFILE_BASE_PATH)
        self.base_path = raw_base.rstrip("/")
        if self.base_path and not self.base_path.startswith("/"):
            self.base_path = f"/{self.base_path}"

    @property
    def is_configured(self) -> bool:
        return bool(self.server_url and self.token and self.repo_id)

    def _get_headers(self) -> dict:
        return {
            "Authorization": f"Token {self.token}",
            "Accept": "application/json; charset=utf-8",
        }

    def get_upload_link(self, parent_dir: str = "/") -> Optional[str]:
        """Requests an upload link for a target parent directory in Seafile repo."""
        if not self.is_configured:
            return None
        encoded_dir = urllib.parse.quote(parent_dir, safe='/')
        url = f"{self.server_url}/api2/repos/{self.repo_id}/upload-link/?p={encoded_dir}"
        try:
            resp = requests.get(url, headers=self._get_headers(), timeout=10)
            resp.raise_for_status()
            # Seafile returns the link as a JSON-encoded string
            link = resp.json()
            return str(link)
        except Exception as e:
            logger.error(f"Failed to get Seafile upload link for {parent_dir}: {e}")
            return None

    def get_update_link(self, parent_dir: str = "/") -> Optional[str]:
        """Requests an update link for an existing file in target parent directory."""
        if not self.is_configured:
            return None
        encoded_dir = urllib.parse.quote(parent_dir, safe='/')
        url = f"{self.server_url}/api2/repos/{self.repo_id}/update-link/?p={encoded_dir}"
        try:
            resp = requests.get(url, headers=self._get_headers(), timeout=10)
            resp.raise_for_status()
            link = resp.json()
            return str(link)
        except Exception as e:
            logger.error(f"Failed to get Seafile update link for {parent_dir}: {e}")
            return None

    def upload_note(
        self,
        category: str,
        filename: str,
        content: Union[str, bytes],
        replace: bool = True,
    ) -> bool:
        """Uploads a Markdown note to the category subfolder under base_path.

        Args:
            category: Note category, e.g. 'Appointments', 'Life', 'Technical'
            filename: Target file name, e.g. '2026-09-06_Meeting.md'
            content: Note text content
            replace: Whether to replace existing file (defaults to True)
        """
        if not self.is_configured:
            logger.warning("Seafile client not configured. Skipping remote vault upload.")
            return False

        parent_dir = f"{self.base_path}/{category.strip('/')}"
        upload_url = self.get_upload_link(parent_dir)
        if not upload_url:
            return False

        if isinstance(content, str):
            content_bytes = content.encode("utf-8")
        else:
            content_bytes = content

        files = {
            "file": (filename, content_bytes, "text/markdown")
        }
        data = {
            "parent_dir": parent_dir,
            "replace": 1 if replace else 0,
        }

        try:
            resp = requests.post(upload_url, files=files, data=data, timeout=20)
            resp.raise_for_status()
            logger.info(f"Synced note to Seafile Vault: {parent_dir}/{filename}")
            return True
        except Exception as e:
            logger.error(f"Failed to upload note {filename} to Seafile ({parent_dir}): {e}")
            return False

    def update_note(
        self,
        category: str,
        filename: str,
        content: Union[str, bytes],
    ) -> bool:
        """Updates an existing Markdown note in the category subfolder.

        In Seafile, upload with replace=1 cleanly updates or creates the file.
        """
        return self.upload_note(category, filename, content, replace=True)


# Default module-level client singleton
default_client = SeafileVaultClient()
