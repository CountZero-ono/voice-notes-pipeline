---
title: "Feature: Move Processed Audio Recordings to Archive"
status: pending
tags: ["#voice-notes-pipeline", "#status/pending"]
---
Modify `voice_harvester.py` to move processed audio files out of the Obsidian Vault directory and into a separate archive storage folder.

Requirements:
1. Define an archive directory (e.g. `ARCHIVE_DIR`), customizable via environment variable `VOICE_ARCHIVE_DIR` (default: `/home/fuad/Seafile/VoiceNotesArchive/`).
2. After successfully transcribing and processing an audio file, move it from `RAW_DIR` to `ARCHIVE_DIR` instead of keeping it in `RAW_DIR`.
3. Create the `ARCHIVE_DIR` folder automatically if it doesn't exist.
4. Ensure the state ledger (`processed_files.json`) and watcher loop handle the move gracefully without double-processing or errors.
