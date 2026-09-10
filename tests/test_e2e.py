#!/usr/bin/env python3
import os
import shutil
import unittest
import tempfile
import yaml
import voice_harvester
import signal_ingest

class TestVoiceNotesPipelineE2E(unittest.TestCase):
    def setUp(self):
        # Create a temporary sandbox directory
        self.test_dir = tempfile.mkdtemp()
        
        self.sandbox_raw = os.path.join(self.test_dir, "Raw")
        self.sandbox_inbox = os.path.join(self.test_dir, "Inbox")
        self.sandbox_archive = os.path.join(self.test_dir, "Archive")
        self.sandbox_state = os.path.join(self.test_dir, "processed_files.json")
        
        os.makedirs(self.sandbox_raw, exist_ok=True)
        os.makedirs(self.sandbox_inbox, exist_ok=True)
        os.makedirs(self.sandbox_archive, exist_ok=True)
        
        # Override module-level directories
        self.orig_raw = voice_harvester.RAW_DIR
        self.orig_inbox = voice_harvester.INBOX_DIR
        self.orig_archive = voice_harvester.ARCHIVE_DIR
        self.orig_state = voice_harvester.STATE_FILE
        self.orig_dry_run = voice_harvester.DRY_RUN
        self.orig_signal_inbox = signal_ingest.INBOX_DIR
        
        voice_harvester.RAW_DIR = self.sandbox_raw
        voice_harvester.INBOX_DIR = self.sandbox_inbox
        voice_harvester.ARCHIVE_DIR = self.sandbox_archive
        voice_harvester.STATE_FILE = self.sandbox_state
        signal_ingest.INBOX_DIR = self.sandbox_inbox
        
        # Enable dry-run by default unless TEST_LIVE is set
        if os.environ.get("TEST_LIVE") == "1":
            voice_harvester.DRY_RUN = False
        else:
            voice_harvester.DRY_RUN = True
            
        # Path to our audio fixture
        self.fixture_path = os.path.join(
            os.path.dirname(__file__), "fixtures", "20260712_103726.m4a"
        )
        self.assertTrue(os.path.exists(self.fixture_path), "Audio fixture file missing!")

    def tearDown(self):
        # Restore original settings
        voice_harvester.RAW_DIR = self.orig_raw
        voice_harvester.INBOX_DIR = self.orig_inbox
        voice_harvester.ARCHIVE_DIR = self.orig_archive
        voice_harvester.STATE_FILE = self.orig_state
        voice_harvester.DRY_RUN = self.orig_dry_run
        signal_ingest.INBOX_DIR = self.orig_signal_inbox
        
        # Clean sandbox
        shutil.rmtree(self.test_dir)

    def test_pipeline_e2e_flow(self):
        # 1. Simulate new audio file ingestion by copying fixture to Raw
        dest_audio = os.path.join(self.sandbox_raw, "test_recording.m4a")
        shutil.copy(self.fixture_path, dest_audio)
        self.assertTrue(os.path.exists(dest_audio))
        
        # 2. Process file (transcribe & cleanup & archive)
        success = voice_harvester.process_file(dest_audio)
        self.assertTrue(success, "process_file failed")
        
        # Verify the raw audio file was moved to the archive directory
        self.assertFalse(os.path.exists(dest_audio), "Raw audio file was not removed from Raw directory after processing")
        archived_file = os.path.join(self.sandbox_archive, "test_recording.m4a")
        self.assertTrue(os.path.exists(archived_file), "Processed audio file was not found in the Archive directory")
        
        # 3. Verify processed note routing and creation
        found_notes = []
        for root, dirs, files in os.walk(self.sandbox_inbox):
            for file in files:
                if file.endswith(".md") and file.startswith("VoiceNote-"):
                    found_notes.append(os.path.join(root, file))
                    
        self.assertGreater(len(found_notes), 0, "No processed markdown notes created!")
        
        note_path = found_notes[0]
        with open(note_path, "r", encoding="utf-8") as f:
            note_content = f.read()
            
        fm, body = voice_harvester.extract_frontmatter_and_body(note_content)
        self.assertTrue(fm, "Frontmatter dictionary is empty")
        
        # Check if the note is an appointment and has "pending" status
        categories = fm.get("categories", [])
        if "appointments" in categories:
            self.assertEqual(fm.get("status"), "pending", "Pending status missing from appointment note")
            
            # 4. Simulate user approval by updating status to approved
            fm["status"] = "approved"
            new_yaml = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)
            approved_content = f"---\n{new_yaml}---\n{body.lstrip()}"
            voice_harvester.atomic_write(note_path, approved_content)
                
            # 5. Run the sync loop to sync approved notes
            voice_harvester.check_and_sync_approved_notes()
            
            # 6. Verify status updated to synced
            with open(note_path, "r", encoding="utf-8") as f:
                final_content = f.read()
            final_fm, _ = voice_harvester.extract_frontmatter_and_body(final_content)
            self.assertEqual(final_fm.get("status"), "synced", "Note status was not updated to synced after sync run!")

    def test_yaml_and_event_parsers(self):
        sample_markdown = """---
categories:
  - appointments
  - technical
title: "Project Sync"
date: "2026-08-20"
startTime: "14:00"
endTime: "15:00"
allDay: false
---
# Transcript
Discussion about pipeline.

# Extracted Tasks
- [ ] Deploy new version 📅 2026-08-20
"""
        fm, body = voice_harvester.extract_frontmatter_and_body(sample_markdown)
        self.assertEqual(fm.get("title"), "Project Sync")
        self.assertEqual(fm.get("categories"), ["appointments", "technical"])
        
        event = voice_harvester.parse_event_from_frontmatter(sample_markdown)
        self.assertIsNotNone(event)
        self.assertEqual(event["title"], "Project Sync")
        self.assertEqual(event["date"], "2026-08-20")
        self.assertEqual(event["startTime"], "14:00")
        self.assertFalse(event["allDay"])
        
        tasks = voice_harvester.parse_tasks_from_markdown(sample_markdown)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["title"], "Deploy new version")
        self.assertEqual(tasks[0]["due_date"], "2026-08-20")

    def test_atomic_write_and_state(self):
        test_file = os.path.join(self.sandbox_inbox, "atomic_test.md")
        content = "---\ntitle: Atomic Test\n---\nHello World"
        voice_harvester.atomic_write(test_file, content)
        self.assertTrue(os.path.exists(test_file))
        with open(test_file, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), content)
            
        test_state = {"test_audio.m4a": {"size": 12345, "processed_at": "2026-08-15"}}
        voice_harvester.save_state(test_state)
        loaded = voice_harvester.load_state()
        self.assertEqual(loaded.get("test_audio.m4a", {}).get("size"), 12345)

    def test_agent_backlog_routing(self):
        sample_agent_llm = """---
categories:
  - agent
  - technical
status: pending
---
# Target / Context
[[voice-notes-pipeline]]

# Requested Agent Action
Fix Russian extraction regression by enforcing English in system prompt.

# Actionable Tasks
- [ ] Update system prompt
- [ ] Verify test suite
"""
        cats = voice_harvester.parse_categories_from_llm(sample_agent_llm)
        self.assertIn("agent", cats)
        self.assertIn("technical", cats)

        created_files = voice_harvester.write_to_inbox(
            "test_agent_note.ogg",
            "ru (99%)",
            "Gemini поправь русскую экстракцию",
            sample_agent_llm
        )
        self.assertEqual(len(created_files), 2)

        # Verify AgentBacklog copy exists and has pending status + agent-backlog tag
        agent_copy = [f for f in created_files if "AgentBacklog" in f]
        self.assertEqual(len(agent_copy), 1)
        with open(agent_copy[0], "r", encoding="utf-8") as f:
            content = f.read()
        fm, body = voice_harvester.extract_frontmatter_and_body(content)
        self.assertEqual(fm.get("status"), "pending")
        self.assertIn("agent-backlog", fm.get("tags", []))
        self.assertIn("voice-notes-pipeline", body)

if __name__ == "__main__":
    unittest.main()
