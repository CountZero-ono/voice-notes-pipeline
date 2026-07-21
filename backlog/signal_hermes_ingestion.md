---
title: "Signal + Hermes Voice Notes Ingestion Bridge"
status: completed
tags: ["#voice-notes-pipeline", "#status/completed"]
---

Implement the Signal + Hermes voice notes ingestion pipeline:
1. Receive incoming Signal voice notes via `signal-cli-rest-api` / Hermes webhook listener.
2. Feed raw audio attachments directly into `voice_harvester.py` (Whisper ASR + Qwen 3.6 processing).
3. Stage formatted notes in Obsidian Vault `Inbox/` with `status: pending`.
4. Automatically archive raw audio files directly to `/mnt/RAID5/VoiceNotesArchive/`.
