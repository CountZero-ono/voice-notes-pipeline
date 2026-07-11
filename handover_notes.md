# Handover Notes: Voice Notes Pipeline & Agent Backlog Integration

This file serves as the briefing note for the next AI agent session.

## 1. Current Project State
*   **Radicale CalDAV Server:** Fully operational on Proxmox host `virtsrv3` as LXC container ID `110` at static IP `192.168.1.30:5232`.
    *   **Calendar Collection:** `http://192.168.1.30:5232/fuad/64e71687-ed01-f827-c34f-38222fd871f5/`
    *   **Tasks/Reminders Collection:** `http://192.168.1.30:5232/fuad/a88e07f8-1c04-17c6-6a6c-5be1c5bf0879/`
    *   **Credentials:** `fuad` / `RadicaleTemp123!` (temporary).
*   **Harvester Daemon (`voice_harvester.py`):**
    *   Saves processed audio transcripts to `/Inbox/Appointments/` with `status: "pending"` in the YAML frontmatter instead of auto-pushing.
    *   Runs a background monitor thread that checks for notes with `status: "approved"`, extracts their events/tasks, formats them as standard iCalendar (`.ics`) payloads, pushes them to the Radicale server, and updates the frontmatter to `status: "synced"`.
    *   Supports multi-category notes by cross-posting duplicates across staging folders and automatically inserting Obsidian backlinks to cross-reference copies.
    *   Daemon is active and running as a local systemd user service (`voice-notes-pipeline.service`).
*   **Documentation:** Fully updated in Obsidian (`voice_notes_pipeline_setup.md` outlining DAVx5, Round Sync, and Tasks.org phone apps) and global topology (`homelab_topology.md`).

---

## 2. Next Steps: Agent Backlog Integration Setup
The user wants to establish a persistent backlog queue between their Obsidian thoughts and AI coding agents. 

Your task in the next session is to set this up:

### Step 2.1: Create the Backlog Directory
*   Create a `backlog/` folder in the root of the project: `/home/fuad/OCProjects/voice-notes-pipeline/backlog/`.
*   This folder will store markdown files for features and fixes that the user dictates or writes.

### Step 2.2: Configure Agent Rules
Create the following agent rule files in the root of `/home/fuad/OCProjects/voice-notes-pipeline/`:

1.  **`.antigravity.md`** (for Antigravity):
    *   Instruct the model to proactively scan the `backlog/` folder at the start of any workspace session.
    *   If any note is found with `status: pending` (or tag `#status/pending`), the model must present these tasks to the user, implement the requested code changes, and update the note to `status: completed` once done.
2.  **`AGENTS.md`** (for OpenCode / Qwen):
    *   Provide the same set of instructions so the Qwen model follows the exact same workflow when opened in this workspace.

### Step 2.3: Test the Setup
Create a test backlog note (e.g. `backlog/add_dry_run_flag.md`) with:
```yaml
---
title: "Feature Request: Dry Run Mode"
status: pending
tags: ["#voice-notes-pipeline", "#status/pending"]
---
Implement a `--dry-run` CLI flag in `voice_harvester.py` to allow testing the daemon flow without making external requests.
```
Verify that the model reads this note on startup and offers to implement it!
Once implemented, the model must update the note to `status: completed`.
