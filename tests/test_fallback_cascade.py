#!/usr/bin/env python3
"""
Test Suite: Voice Notes Pipeline Failover & Fallback Cascades
1. STT Failover: Local Faster-Whisper failure -> Groq Cloud Whisper API.
2. LLM Tier-2 Failover: Local Qwen failure -> b.ai Cloud Qwen (qwen3.8-flash).
3. LLM Tier-3 Failover: Local Qwen & b.ai failure -> Vertex AI Gemini 3.7 Flash.
4. End-to-End Pipeline Failover: Full audio -> transcription -> note creation under failure.
"""

import os
import sys
import shutil
import unittest
import tempfile

# Ensure parent directory is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voice_harvester


class TestVoiceNotesFallbackCascade(unittest.TestCase):
    def setUp(self):
        # Create isolated temporary workspace
        self.test_dir = tempfile.mkdtemp()
        self.sandbox_raw = os.path.join(self.test_dir, "Raw")
        self.sandbox_inbox = os.path.join(self.test_dir, "Inbox")
        self.sandbox_archive = os.path.join(self.test_dir, "Archive")
        self.sandbox_state = os.path.join(self.test_dir, "processed_files.json")

        os.makedirs(self.sandbox_raw, exist_ok=True)
        os.makedirs(self.sandbox_inbox, exist_ok=True)
        os.makedirs(self.sandbox_archive, exist_ok=True)

        # Snapshot original config
        self.orig_raw = voice_harvester.RAW_DIR
        self.orig_inbox = voice_harvester.INBOX_DIR
        self.orig_archive = voice_harvester.ARCHIVE_DIR
        self.orig_state = voice_harvester.STATE_FILE
        self.orig_dry_run = voice_harvester.DRY_RUN
        self.orig_load_whisper = voice_harvester.load_whisper
        self.orig_llm_url = voice_harvester.LLM_API_URL
        self.orig_bai_url = voice_harvester.BAI_API_URL
        self.orig_openrouter_url = getattr(voice_harvester, "OPENROUTER_API_URL", "")
        self.orig_failover_provider = voice_harvester.CLOUD_FAILOVER_PROVIDER
        self.orig_bama_gateway_url = getattr(voice_harvester, "BAMA_GATEWAY_URL", "")

        # Configure sandboxed environment
        voice_harvester.RAW_DIR = self.sandbox_raw
        voice_harvester.INBOX_DIR = self.sandbox_inbox
        voice_harvester.ARCHIVE_DIR = self.sandbox_archive
        voice_harvester.STATE_FILE = self.sandbox_state
        voice_harvester.DRY_RUN = False

        self.orig_gateway_fn = getattr(voice_harvester, "get_gateway_url", None)
        voice_harvester.get_gateway_url = lambda: "http://127.0.0.1:59997/v1"

        self.fixture_path = os.path.join(
            os.path.dirname(__file__), "fixtures", "20260712_103726.m4a"
        )
        self.assertTrue(os.path.exists(self.fixture_path), "Fixture audio file missing!")

    def tearDown(self):
        # Restore configuration
        if self.orig_gateway_fn:
            voice_harvester.get_gateway_url = self.orig_gateway_fn
        voice_harvester.RAW_DIR = self.orig_raw
        voice_harvester.INBOX_DIR = self.orig_inbox
        voice_harvester.ARCHIVE_DIR = self.orig_archive
        voice_harvester.STATE_FILE = self.orig_state
        voice_harvester.DRY_RUN = self.orig_dry_run
        voice_harvester.load_whisper = self.orig_load_whisper
        voice_harvester.LLM_API_URL = self.orig_llm_url
        voice_harvester.BAI_API_URL = self.orig_bai_url
        voice_harvester.OPENROUTER_API_URL = self.orig_openrouter_url
        voice_harvester.CLOUD_FAILOVER_PROVIDER = self.orig_failover_provider
        voice_harvester.BAMA_GATEWAY_URL = self.orig_bama_gateway_url

        # Clean up sandbox
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_1_stt_fallback_to_groq(self):
        """Simulate local Faster-Whisper failure and assert Groq Whisper transcribes successfully."""
        voice_harvester.BAMA_GATEWAY_URL = ""

        def mock_broken_whisper():
            raise RuntimeError("Simulated Faster-Whisper CPU crash / OOM")

        voice_harvester.load_whisper = mock_broken_whisper

        transcript, lang, conf = voice_harvester.transcribe_audio(self.fixture_path)
        self.assertTrue(bool(transcript), "Groq failover transcript should not be empty")
        self.assertIn(lang.lower(), ["ru", "russian", "auto"], f"Expected Russian or auto, got: {lang}")
        self.assertGreaterEqual(conf, 0.9, f"Expected high confidence, got: {conf}")

    def test_2_llm_fallback_to_bai_qwen(self):
        """Simulate local Qwen failure and assert Tier-2 b.ai Cloud Qwen takes over."""
        voice_harvester.LLM_API_URL = "http://127.0.0.1:59999/v1/chat/completions"
        voice_harvester.CLOUD_FAILOVER_PROVIDER = "qwen_cloud"

        sample_prompt = (
            "Напомни мне завтра в 15:00 встретиться с Расимом для проверки бэкапов на virtsrv3. "
            "И купить патч-корды Cat6."
        )

        res = voice_harvester.clean_and_extract_llm(sample_prompt, max_retries=1, initial_backoff=0.1)
        self.assertIsNotNone(res, "b.ai Qwen failover returned None")
        fm, body = voice_harvester.extract_frontmatter_and_body(res)
        self.assertTrue(bool(fm), "Failed to extract YAML frontmatter from b.ai response")
        self.assertIn("title", fm, "Frontmatter missing 'title'")

    def test_3_llm_fallback_to_vertex_gemini(self):
        """Simulate local Qwen AND b.ai failure, assert Tier-3 Vertex AI Gemini takes over."""
        voice_harvester.LLM_API_URL = "http://127.0.0.1:59999/v1/chat/completions"
        voice_harvester.BAI_API_URL = "http://127.0.0.1:59998/v1/chat/completions"
        voice_harvester.OPENROUTER_API_URL = "http://127.0.0.1:59998/v1/chat/completions"
        voice_harvester.CLOUD_FAILOVER_PROVIDER = "qwen_cloud"

        sample_prompt = (
            "Завтра в 11:00 нужно настроить AdGuard на virtsrv3. Срочная задача."
        )

        res = voice_harvester.clean_and_extract_llm(sample_prompt, max_retries=1, initial_backoff=0.1)
        self.assertIsNotNone(res, "Vertex AI Gemini failover returned None")
        fm, body = voice_harvester.extract_frontmatter_and_body(res)
        self.assertTrue(bool(fm), "Failed to extract YAML frontmatter from Vertex AI response")
        self.assertIn("title", fm, "Frontmatter missing 'title'")

    def test_4_e2e_full_failover_flow(self):
        """Simulate local Whisper AND local Qwen down; test end-to-end processing into Obsidian note."""
        def mock_broken_whisper():
            raise RuntimeError("Simulated Faster-Whisper failure")

        voice_harvester.load_whisper = mock_broken_whisper
        voice_harvester.LLM_API_URL = "http://127.0.0.1:59999/v1/chat/completions"
        voice_harvester.CLOUD_FAILOVER_PROVIDER = "qwen_cloud"

        dest_audio = os.path.join(self.sandbox_raw, "failover_test.m4a")
        shutil.copy(self.fixture_path, dest_audio)

        success = voice_harvester.process_file(dest_audio)
        self.assertTrue(success, "process_file failed during failover cascade")

        # Verify raw was moved to Archive
        self.assertFalse(os.path.exists(dest_audio), "Raw audio should be removed from Raw/")
        archived_file = os.path.join(self.sandbox_archive, "failover_test.m4a")
        self.assertTrue(os.path.exists(archived_file), "Audio should be present in Archive/")

        # Verify Markdown note in Inbox (recursing into category subdirectories)
        found_notes = []
        for root, dirs, files in os.walk(self.sandbox_inbox):
            for file in files:
                if file.endswith(".md") and file.startswith("VoiceNote-"):
                    found_notes.append(os.path.join(root, file))
        self.assertGreaterEqual(len(found_notes), 1, "Expected at least 1 VoiceNote markdown file in Inbox tree")
        note_path = found_notes[0]
        with open(note_path, "r", encoding="utf-8") as f:
            content = f.read()

        fm, body = voice_harvester.extract_frontmatter_and_body(content)
        self.assertTrue(bool(fm), "Frontmatter should be valid in generated note")
        self.assertIn("title", fm)
        self.assertIn("categories", fm)


if __name__ == "__main__":
    unittest.main()
