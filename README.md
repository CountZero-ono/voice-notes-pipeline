# Voice Notes Thought Conveyor (Мыслеконвейер)

An automated, local, zero-cloud voice-to-thought pipeline that ingests voice messages over Signal, transcribes them using `faster-whisper`, structures trilingual text via local LLMs (Qwen 3.6 35B), auto-routes formatted notes into an Obsidian Vault (`Appointments`, `Technical`, `Life`), and provides zero-VPN interactive calendar management (`approve`, `reject`, `reschedule`) synced directly to Radicale CalDAV.

---

## Architecture Overview

```
[ Mobile / Signal App ] ──(Voice Note / Text)──► [ signal-cli-rest-api (Port 8080) ]
                                                            │
                                                            ▼ (WebSocket Stream)
                                            [ signal_ingest.py Listener ]
                                                            │
                                                            ▼
                                           [ faster-whisper ASR (CPU int8) ]
                                                            │
                                                            ▼
                                           [ Qwen 3.6 35B (Port 1235) ]
                                                            │
                                                            ├──► Obsidian Vault Inbox (/Inbox)
                                                            ├──► RAID5 Raw Archive (/VoiceNotesArchive)
                                                            └──► Interactive Signal Alerts & Actions
                                                                  (Approve / Reject / Reschedule)
                                                                        │
                                                                        ▼
                                                              [ Radicale CalDAV Server ]
```

---

## Core Features

- **Trilingual Speech-to-Text:** Converts English, Russian, and Azerbaijani voice notes automatically using `faster-whisper` (`large-v3-turbo` model on CPU).
- **LLM Cleanup & Feature Extraction:** Removes spoken filler words (e.g. *"новая строка"*, *"yeni sətir"*), extracts YAML frontmatter, tasks (`- [ ]`), and structured markdown summaries.
- **Priority Multi-Folder Routing:** Routes notes into Obsidian subfolders:
  - `Inbox/Appointments/` — Deadlines, meetings, tasks (`status: pending`).
  - `Inbox/Technical/` — Code snippets, CLI specs, project facts.
  - `Inbox/Life/` — Daily logs, journals, general thoughts.
- **Zero-VPN Mobile Control:** Receive instant 2-stage Signal receipts (`⏳ Processing...` ──► `✅ Staged!`), live Radicale CalDAV conflict checks (`🟢 Free` / `⚠️ Conflict`), and manage events directly in Signal text replies (`approve`, `reject`, `15:30`).
- **Data Sovereignty:** 100% cloud-free runtime. Raw audio files move straight to `/mnt/RAID5/VoiceNotesArchive/`.

---

## Directory Structure

* **[voice_harvester.py](file:///home/fuad/OCProjects/voice-notes-pipeline/voice_harvester.py):** Main transcription, categorization, and CalDAV sync engine.
* **[signal_ingest.py](file:///home/fuad/OCProjects/voice-notes-pipeline/signal_ingest.py):** Real-time WebSocket Signal message listener and interactive command handler.
* **[system_prompt.md](file:///home/fuad/OCProjects/voice-notes-pipeline/system_prompt.md):** Instruction template for the local LLM post-processing layer.
* **[backlog/](file:///home/fuad/OCProjects/voice-notes-pipeline/backlog/):** Task queue for backlog automation.
* **[tests/](file:///home/fuad/OCProjects/voice-notes-pipeline/tests/):** End-to-end integration test suite (`python3 -m unittest discover -s tests`).

---

## Deploying for Yourself or a Friend

### Option A: The Sovereign Homelab Stack (Local AI + Radicale + Obsidian)

1. **Start the Signal Gateway (Docker):**
   ```bash
   docker run -d \
     --name signal-cli-rest-api \
     --restart unless-stopped \
     -p 8080:8080 \
     -v ~/.local/share/signal-cli:/home/pb/signal-cli-config \
     -e MODE=json-rpc \
     bbernhard/signal-cli-rest-api:latest
   ```
2. **Link Your Signal Account:**
   Open `http://localhost:8080/v1/qrcodelink?device_name=Hermes-SER7` in your browser and scan the QR code using **Signal (Settings $\rightarrow$ Linked Devices $\rightarrow$ Link New Device)**.

3. **Configure Environment Variables & Credentials:**
   ```bash
   export SIGNAL_PHONE_NUMBER="+1234567890"
   export LLM_API_URL="http://127.0.0.1:1235/v1/chat/completions"
   export RADICALE_CALENDAR_URL="http://192.168.1.30:5232/user/calendar/"
   export GCAL_CREDENTIALS="/home/fuad/OCProjects/voice-notes-pipeline/gcal_credentials.json"
   ```
   *(Optional: Place your Google Cloud Service Account `gcal_credentials.json` file in the repo directory for automatic Google Calendar & Tasks dual-sync).*

4. **Launch the Listener:**
   ```bash
   python3 signal_ingest.py
   ```

### Option B: The Cloud / Non-Technical Friend Setup (Gemini + Google Calendar)

If deploying for a non-technical friend with a **Google AI Pro** subscription:
- **STT + LLM:** Replace local Whisper and local Qwen with **Gemini API** (`gemini-2.0-flash` or `gemini-1.5-pro`). Feed raw audio files directly into Gemini in a single API call.
- **Calendar & Notes:** Route appointments directly to **Google Calendar API** and save notes to **Google Drive / Google Docs / Notion / Obsidian (via iCloud/Google Drive)**.

---

## Testing & Maintenance

### Run Dry-Run Mode (Offline Test)
```bash
python3 voice_harvester.py --dry-run
```

### Run End-to-End Test Suite
```bash
python3 -m unittest discover -s tests
```
