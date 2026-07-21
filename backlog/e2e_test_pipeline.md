---
title: "E2E Test of Recording-Transcription-Processing Pipeline"
status: completed
tags: ["#voice-notes-pipeline", "#status/completed"]
---
Implement an end-to-end (E2E) integration test to verify the entire voice notes pipeline. 
The test should cover:
1. Simulating an audio recording input by placing a sample audio file (e.g. mp3/wav) in the Raw directory.
2. Verifying that the Whisper transcription module successfully runs and transcribes the audio.
3. Verifying that the LLM processing cleans the transcript and extracts frontmatter correctly.
4. Verifying that the processed markdown file is saved inside the correct Inbox category.
5. Verifying that CalDAV syncing triggers correctly when the note is marked as "approved".
6. Testing different types of recordings (e.g. pure scheduling notes, technical specifications, simple daily logs, and multi-category notes) to ensure they are routed and cross-posted to the correct folders in the Vault Inbox.

