"""
Unit tests for SeafileVaultClient in seafile_sync.py
"""

import unittest
from unittest.mock import patch, MagicMock
import os
import sys

# Ensure module path is accessible
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from seafile_sync import SeafileVaultClient


class TestSeafileVaultClient(unittest.TestCase):

    def setUp(self):
        self.client = SeafileVaultClient(
            server_url="https://seafile.test.net",
            token="test-token-12345",
            repo_id="repo-uuid-test",
            base_path="/VoiceNotes/Inbox",
        )

    def test_is_configured(self):
        self.assertTrue(self.client.is_configured)
        unconfigured = SeafileVaultClient(server_url="", token="", repo_id="")
        self.assertFalse(unconfigured.is_configured)

    @patch("requests.get")
    def test_get_upload_link_success(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = "https://seafile.test.net/seafhttp/upload-api/abc123xyz"
        mock_get.return_value = mock_resp

        link = self.client.get_upload_link("/VoiceNotes/Inbox/Appointments")
        self.assertEqual(link, "https://seafile.test.net/seafhttp/upload-api/abc123xyz")
        mock_get.assert_called_once()
        args, kwargs = mock_get.call_args
        self.assertIn("/api2/repos/repo-uuid-test/upload-link/?p=/VoiceNotes/Inbox/Appointments", args[0])
        self.assertEqual(kwargs["headers"]["Authorization"], "Token test-token-12345")

    @patch("requests.get")
    @patch("requests.post")
    def test_upload_note_success(self, mock_post, mock_get):
        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 200
        mock_get_resp.json.return_value = "https://seafile.test.net/seafhttp/upload-api/abc123xyz"
        mock_get.return_value = mock_get_resp

        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 200
        mock_post.return_value = mock_post_resp

        content = "---\ntitle: Test\n---\nHello Seafile"
        success = self.client.upload_note("Appointments", "2026-09-06_Test.md", content)

        self.assertTrue(success)
        mock_get.assert_called_once()
        mock_post.assert_called_once()
        post_args, post_kwargs = mock_post.call_args
        self.assertEqual(post_args[0], "https://seafile.test.net/seafhttp/upload-api/abc123xyz")
        self.assertIn("file", post_kwargs["files"])
        self.assertEqual(post_kwargs["data"]["parent_dir"], "/VoiceNotes/Inbox/Appointments")
        self.assertEqual(post_kwargs["data"]["replace"], 1)

    @patch("requests.get")
    def test_upload_note_link_failure(self, mock_get):
        mock_get.side_effect = Exception("Connection refused")
        success = self.client.upload_note("Appointments", "fail.md", "content")
        self.assertFalse(success)


if __name__ == "__main__":
    unittest.main()
