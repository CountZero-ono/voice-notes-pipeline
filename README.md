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
*   **Staging Inbox:** `[VoiceNotes/Inbox/](file:///home/fuad/Seafile/Obsidian%20Vaults/VoiceNotes/Inbox/)` (Review staging area with subfolders):
    *   `[Inbox/Appointments/](file:///home/fuad/Seafile/Obsidian%20Vaults/VoiceNotes/Inbox/Appointments/)` — Tasks, deadlines, calendar events.
    *   `[Inbox/Technical/](file:///home/fuad/Seafile/Obsidian%20Vaults/VoiceNotes/Inbox/Technical/)` — CLI logs, specs, codes, and structured facts.
    *   `[Inbox/Life/](file:///home/fuad/Seafile/Obsidian%20Vaults/VoiceNotes/Inbox/Life/)` — General journals, life notes, fallbacks.
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

## 4. How the Clean-up & Routing Works

The LLM is prompted to perform three operations:
1.  **Paragraph Break Conversion:** Translate spoken paragraph cues (e.g. *"yeni sətir"*, *"новая строка"*, *"new line"*) into physical newlines (`\n`).

2.  **Classification & Multi-Tagging:** Identify all categories that apply to a note and save them under the `categories` property in the frontmatter.
3.  **Content Structuring:**
    *   `appointments`: Formats tasks (`- [ ] Task Description 📅 YYYY-MM-DD`) and full calendar events in the YAML frontmatter.
    *   `technical`: Synthesizes transcript into structured markdown sections, highlighting commands and parameters, and adding a `# Key Knowledge & Facts` summary.
    *   `life`: Simple cleaned transcription logs.

### Priority Routing Hierarchy
The physical markdown file is routed into one subfolder based on the highest matched category:
$$\text{Appointments (Priority 1)} \rightarrow \text{Technical (Priority 2)} \rightarrow \text{Life (Priority 3/Fallback)}$$
This preserves single-source-of-truth file architecture while retaining all semantic category tags inside the frontmatter for Obsidian search/indexing.
