#!/usr/bin/env python3
import os
import shutil
import unittest
import tempfile
import voice_harvester

class TestVoiceNotesPipelineE2E(unittest.TestCase):
    def setUp(self):
        # Create a temporary sandbox directory
        self.test_dir = tempfile.mkdtemp()
        
        self.sandbox_raw = os.path.join(self.test_dir, "Raw")
        self.sandbox_inbox = os.path.join(self.test_dir, "Inbox")
        self.sandbox_state = os.path.join(self.test_dir, "processed_files.json")
        
        os.makedirs(self.sandbox_raw, exist_ok=True)
        os.makedirs(self.sandbox_inbox, exist_ok=True)
        
        # Override voice_harvester module-level directories
        self.orig_raw = voice_harvester.RAW_DIR
        self.orig_inbox = voice_harvester.INBOX_DIR
        self.orig_state = voice_harvester.STATE_FILE
        self.orig_dry_run = voice_harvester.DRY_RUN
        
        voice_harvester.RAW_DIR = self.sandbox_raw
        voice_harvester.INBOX_DIR = self.sandbox_inbox
        voice_harvester.STATE_FILE = self.sandbox_state
        
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
        voice_harvester.STATE_FILE = self.orig_state
        voice_harvester.DRY_RUN = self.orig_dry_run
        
        # Clean sandbox
        shutil.rmtree(self.test_dir)

    def test_pipeline_e2e_flow(self):
        # 1. Simulate new audio file ingestion by copying fixture to Raw
        dest_audio = os.path.join(self.sandbox_raw, "test_recording.m4a")
        shutil.copy(self.fixture_path, dest_audio)
        self.assertTrue(os.path.exists(dest_audio))
        
        # 2. Process file (transcribe & cleanup)
        success = voice_harvester.process_file(dest_audio)
        self.assertTrue(success, "process_file failed")
        
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
            
        self.assertIn("---", note_content, "Frontmatter not found in note")
        
        # Check if the note is an appointment and has "pending" status
        is_appointment = "appointments" in note_content.lower()
        if is_appointment:
            self.assertIn('status: "pending"', note_content, "Pending status missing from appointment note")
            
            # 4. Simulate user approval by updating status to approved
            approved_content = note_content.replace('status: "pending"', 'status: "approved"')
            with open(note_path, "w", encoding="utf-8") as f:
                f.write(approved_content)
                
            # 5. Run the sync loop to sync approved notes
            voice_harvester.check_and_sync_approved_notes()
            
            # 6. Verify status updated to synced
            with open(note_path, "r", encoding="utf-8") as f:
                final_content = f.read()
            self.assertIn('status: "synced"', final_content, "Note status was not updated to synced after sync run!")
            
        else:
            # If not an appointment, verify it doesn't have status: pending
            self.assertNotIn("status: ", note_content, "Non-appointment note has status field")

if __name__ == "__main__":
    unittest.main()
