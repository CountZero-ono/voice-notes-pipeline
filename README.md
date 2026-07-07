# Voice Notes Thought Conveyor (Мыслеконвейер)

An automated, local, and cloud-free voice-to-text pipeline that monitors raw audio recordings synced via Seafile, transcribes them using `faster-whisper`, cleans up trilingual formatting commands via a local LLM, and populates task lists and calendar events in an Obsidian Vault.

---

## 1. Project Directory Structure

All scripts, prompts, and logs are isolated inside this directory:

*   **[voice_harvester.py](file:///home/fuad/OCProjects/voice-notes-pipeline/voice_harvester.py):** Main automated watcher and transcription coordinator.
*   **[system_prompt.md](file:///home/fuad/OCProjects/voice-notes-pipeline/system_prompt.md):** Instruction template for the local LLM post-processing layer.
*   **[harvester.log](file:///home/fuad/OCProjects/voice-notes-pipeline/harvester.log):** Background execution log file.

The script operates on the following Obsidian Vault folders:
*   **Raw Input:** `[VoiceNotes/Raw/](file:///home/fuad/Seafile/Obsidian%20Vaults/VoiceNotes/Raw/)` (Incoming voice files)
*   **Staging Inbox:** `[VoiceNotes/Inbox/](file:///home/fuad/Seafile/Obsidian%20Vaults/VoiceNotes/Inbox/)` (Review staging area)
*   **State Ledger:** `[processed_files.json](file:///home/fuad/Seafile/Obsidian%20Vaults/VoiceNotes/processed_files.json)` (Double-processing guard)

---

## 2. Technical Stack

*   **ASR Engine:** `faster-whisper` (Python implementation of CTranslate2) loading the `large-v3-turbo` model. Restricted to **CPU** with `int8` quantization to avoid VRAM exhaustion alongside large LLMs.
*   **Post-Processor:** Local OpenAI-compatible API backend (defaults to `http://localhost:1235` running Qwen 3.6 35B).
*   **Watcher Logic:** Loop-based polling with file-stabilization safety checks (ignores partially synced Seafile uploads).

---

## 3. Deployment & Control

The pipeline runs as a systemd user-level daemon.

### Service File Location
`[voice-notes-pipeline.service](file:///home/fuad/.config/systemd/user/voice-notes-pipeline.service)`

### Manage Daemon Status
```bash
# Reload changes if you edit the service file
systemctl --user daemon-reload

# Start the harvester
systemctl --user start voice-notes-pipeline.service

# Stop the harvester
systemctl --user stop voice-notes-pipeline.service

# Enable auto-start on user login
systemctl --user enable voice-notes-pipeline.service

# View background logs
systemctl --user status voice-notes-pipeline.service
journalctl --user -u voice-notes-pipeline.service -f
```

---

## 4. How the Clean-up Works

The LLM is prompted to perform two operations:
1.  **Punctuation Conversion:** Translate spoken words (e.g. *"vergül"*, *"запятая"*, *"comma"*) to their respective punctuation symbols (`,`, `.`, `\n`).
2.  **Information Extraction:** Create tasks formatted for the **Obsidian Tasks** plugin (`- [ ] Task Description 📅 YYYY-MM-DD`) and calendars events for **Obsidian Full Calendar** (YAML frontmatter blocks) relative to the note's recording date.
