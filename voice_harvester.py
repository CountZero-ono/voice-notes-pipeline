#!/usr/bin/env python3
"""
Voice Notes Harvester & Thought Conveyor
Processes local multi-lingual voice notes on CPU, classifies and cleans them using a local LLM,
deposits formatted notes in categorized subfolders inside the Obsidian Vault Inbox.
If a note contains appointments/tasks, it sets 'status: pending' in the frontmatter.
When a user approves a note by setting 'status: approved', it automatically pushes the
finalized events and tasks to a Radicale CalDAV server and updates the status to 'synced'.
"""

import os
import sys
import time
import json
import logging
from datetime import datetime
import requests
import uuid

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.expanduser("~/OCProjects/voice-notes-pipeline/harvester.log"))
    ]
)

# Configuration Defaults (Overrides via environment variables)
RAW_DIR = os.environ.get("VOICE_RAW_DIR", "/home/fuad/Seafile/Obsidian Vaults/VoiceNotes/Raw/")
INBOX_DIR = os.environ.get("VOICE_INBOX_DIR", "/home/fuad/Seafile/Obsidian Vaults/VoiceNotes/Inbox/")
STATE_FILE = os.environ.get("VOICE_STATE_FILE", "/home/fuad/Seafile/Obsidian Vaults/VoiceNotes/processed_files.json")
SYSTEM_PROMPT_PATH = os.environ.get("VOICE_SYSTEM_PROMPT", "/home/fuad/OCProjects/voice-notes-pipeline/system_prompt.md")
ARCHIVE_DIR = os.environ.get("VOICE_ARCHIVE_DIR", "/mnt/RAID5/VoiceNotesArchive/")

# LLM Config
LLM_API_URL = os.environ.get("LLM_API_URL", "http://127.0.0.1:1235/v1/chat/completions") # Port 1235 maps to Qwen 35B
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen")

# Whisper Config
WHISPER_MODEL_NAME = os.environ.get("WHISPER_MODEL", "large-v3-turbo")
WHISPER_THREADS = int(os.environ.get("WHISPER_THREADS", "4"))

# Radicale CalDAV Configuration
RADICALE_CALENDAR_URL = os.environ.get("RADICALE_CALENDAR_URL", "http://192.168.1.30:5232/fuad/64e71687-ed01-f827-c34f-38222fd871f5/")
RADICALE_TASKS_URL = os.environ.get("RADICALE_TASKS_URL", "http://192.168.1.30:5232/fuad/a88e07f8-1c04-17c6-6a6c-5be1c5bf0879/")
RADICALE_USER = os.environ.get("RADICALE_USER", "fuad")
RADICALE_PASSWORD = os.environ.get("RADICALE_PASSWORD", "RadicaleTemp123!")
RADICALE_AUTH = (RADICALE_USER, RADICALE_PASSWORD)

# Supported Extensions
SUPPORTED_EXTENSIONS = (".mp3", ".wav", ".m4a")

# Global Whisper Model Instance
whisper_model = None

# Dry run mode flag
DRY_RUN = False

def load_whisper():
    global whisper_model
    if whisper_model is None:
        logging.info("Lazy-loading faster-whisper package...")
        try:
            from faster_whisper import WhisperModel
            logging.info(f"Initializing WhisperModel '{WHISPER_MODEL_NAME}' on CPU...")
            whisper_model = WhisperModel(
                WHISPER_MODEL_NAME,
                device="cpu",
                compute_type="int8",
                cpu_threads=WHISPER_THREADS
            )
            logging.info("Whisper model loaded successfully on CPU.")
        except ImportError:
            logging.critical("faster-whisper is not installed. Run: pip install faster-whisper")
            sys.exit(1)
        except Exception as e:
            logging.critical(f"Failed to load Whisper model: {e}")
            sys.exit(1)
    return whisper_model

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            logging.warning("State file corrupted. Re-initializing.")
            return {}
    return {}

def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=4, ensure_ascii=False)

def wait_for_file_to_stabilize(filepath, check_interval=3, timeout=60):
    logging.info(f"Waiting for file {os.path.basename(filepath)} to stabilize...")
    start_time = time.time()
    last_size = -1
    while time.time() - start_time < timeout:
        try:
            current_size = os.path.getsize(filepath)
            if current_size == last_size and current_size > 0:
                logging.info(f"File size stabilized at {current_size} bytes.")
                return True
            last_size = current_size
        except OSError:
            pass
        time.sleep(check_interval)
    logging.warning(f"File stabilization timed out for {filepath}.")
    return False

def transcribe_audio(filepath):
    model = load_whisper()
    logging.info(f"Transcribing audio: {filepath}")
    segments, info = model.transcribe(filepath, beam_size=5)
    raw_text_parts = []
    for segment in segments:
        raw_text_parts.append(segment.text)
    raw_transcript = " ".join(raw_text_parts).strip()
    logging.info(f"ASR complete. Language detected: {info.language} ({info.language_probability:.2f})")
    return raw_transcript, info.language, info.language_probability

def clean_and_extract_llm(raw_text):
    if DRY_RUN:
        logging.info(f"[DRY RUN] Simulating LLM request to {LLM_API_URL}...")
        today_str = datetime.now().strftime("%Y-%m-%d")
        categories = ["life"]
        raw_lower = raw_text.lower()
        # Trilingual keywords for mock categorization
        appointment_keywords = [
            "appointment", "meeting", "task", "todo", "schedule", "calendar",
            "встреча", "задача", "напомнить", "напоминание", "план", "календарь", "завтра", "записать",
            "görüş", "tapşırıq", "xatırlatma", "təqvim", "sabah"
        ]
        technical_keywords = [
            "code", "database", "ip", "config", "server", "cli",
            "код", "база", "настройка", "конфиг", "сервер",
            "kod", "baza", "server", "quraşdırma"
        ]
        
        if any(w in raw_lower for w in appointment_keywords):
            categories.append("appointments")
        if any(w in raw_lower for w in technical_keywords):
            categories.append("technical")
        
        # Deduplicate and remove "life" if we have other categories
        if len(categories) > 1 and "life" in categories:
            categories.remove("life")
            
        fm_lines = ["categories:"]
        for cat in categories:
            fm_lines.append(f"  - {cat}")
            
        if "appointments" in categories:
            fm_lines.append('title: "Mock Event"')
            fm_lines.append('allDay: false')
            fm_lines.append(f'date: "{today_str}"')
            fm_lines.append('startTime: "10:00"')
            fm_lines.append('endTime: "11:00"')
            
        fm_str = "\n".join(fm_lines)
        
        body_parts = []
        if "appointments" in categories or "life" in categories:
            body_parts.append("# Cleaned Transcript")
            body_parts.append(f"Cleaned: {raw_text}")
            
        if "appointments" in categories:
            body_parts.append("\n# Extracted Tasks")
            body_parts.append(f"- [ ] Mock task from transcript 📅 {today_str}")
            
        if "technical" in categories:
            body_parts.append("\n# Technical Summary")
            body_parts.append(f"Technical summary of: {raw_text}")
            body_parts.append("\n# Key Knowledge & Facts")
            body_parts.append("- Fact: Mock dry run tech fact.")
            
        mock_output = f"---\n{fm_str}\n---\n" + "\n".join(body_parts)
        return mock_output

    if not os.path.exists(SYSTEM_PROMPT_PATH):
        logging.error(f"System prompt file not found at {SYSTEM_PROMPT_PATH}")
        return None
    with open(SYSTEM_PROMPT_PATH, "r", encoding="utf-8") as f:
        system_prompt = f.read()
    today_str = datetime.now().strftime("%Y-%m-%d")
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Today's Reference Date: {today_str}\n\nRaw Transcription Text:\n{raw_text}"}
    ]
    headers = {"Content-Type": "application/json"}
    payload = {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": 0.1,
        "stream": False
    }
    logging.info(f"Sending transcript to local LLM at {LLM_API_URL}...")
    try:
        response = requests.post(LLM_API_URL, headers=headers, json=payload, timeout=120)
        response.raise_for_status()
        res_json = response.json()
        llm_output = res_json['choices'][0]['message']['content']
        logging.info("LLM response received successfully.")
        return llm_output
    except Exception as e:
        logging.error(f"Failed to communicate with local LLM: {e}")
        return None

def parse_categories_from_llm(llm_content):
    content = llm_content.strip()
    if not content.startswith("---"):
        return ["life"]
    parts = content.split("---", 2)
    if len(parts) < 3:
        return ["life"]
    frontmatter = parts[1]
    categories = []
    in_categories_block = False
    for line in frontmatter.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("categories:"):
            rest = line.split(":", 1)[1].strip()
            if rest.startswith("[") and rest.endswith("]"):
                inline_cats = rest[1:-1].split(",")
                for cat in inline_cats:
                    cat = cat.strip().replace('"', '').replace("'", "").lower()
                    if cat in ("appointments", "technical", "life"):
                        categories.append(cat)
                in_categories_block = False
            else:
                in_categories_block = True
            continue
        if in_categories_block:
            if ":" in line and not line.startswith("-"):
                in_categories_block = False
                continue
            if line.startswith("-"):
                cat = line.split("-", 1)[1].strip().replace('"', '').replace("'", "").lower()
                if cat in ("appointments", "technical", "life"):
                    categories.append(cat)
    unique_cats = []
    for c in categories:
        if c not in unique_cats:
            unique_cats.append(c)
    if not unique_cats:
        return ["life"]
    return unique_cats

def parse_event_from_frontmatter(content):
    content = content.strip()
    if not content.startswith("---"):
        return None
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None
    frontmatter = parts[1]
    event_data = {}
    for line in frontmatter.splitlines():
        line = line.strip()
        if ":" in line:
            k, v = line.split(":", 1)
            k = k.strip().lower()
            v = v.strip().replace('"', '').replace("'", "")
            if k in ("title", "date", "starttime", "endtime", "allday"):
                if k == "title":
                    event_data["title"] = v
                elif k == "date":
                    event_data["date"] = v
                elif k == "starttime":
                    event_data["startTime"] = v
                elif k == "endtime":
                    event_data["endTime"] = v
                elif k == "allday":
                    event_data["allDay"] = v.lower() == "true"
    if event_data.get("title") and event_data.get("date"):
        if "allDay" not in event_data:
            event_data["allDay"] = True
        return event_data
    return None

def parse_tasks_from_markdown(content):
    tasks = []
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("- [ ]"):
            title = line[5:].strip()
            due_date = None
            if "📅" in title:
                parts = title.split("📅", 1)
                title = parts[0].strip()
                date_part = parts[1].strip()
                if len(date_part) == 10 and date_part[4] == "-" and date_part[7] == "-":
                    due_date = date_part
            if title:
                tasks.append({'title': title, 'due_date': due_date})
    return tasks

def push_event_to_radicale(title, date_str, start_time=None, end_time=None, all_day=True):
    from datetime import datetime, timedelta
    uid = str(uuid.uuid4())
    dtstamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    title_clean = title.replace('"', '\\"').replace('\n', ' ')
    
    if all_day:
        date_clean = date_str.replace("-", "")
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            end_dt = dt + timedelta(days=1)
            end_date_clean = end_dt.strftime("%Y%m%d")
        except Exception:
            end_date_clean = date_clean
        dtstart_line = f"DTSTART;VALUE=DATE:{date_clean}"
        dtend_line = f"DTEND;VALUE=DATE:{end_date_clean}"
    else:
        start_clean = f"{date_str.replace('-', '')}T{start_time.replace(':', '')}00"
        if end_time:
            end_clean = f"{date_str.replace('-', '')}T{end_time.replace(':', '')}00"
        else:
            try:
                s_dt = datetime.strptime(f"{date_str} {start_time}", "%Y-%m-%d %H:%M")
                e_dt = s_dt + timedelta(hours=1)
                end_clean = e_dt.strftime("%Y%m%dT%H%M%S")
            except Exception:
                end_clean = start_clean
        dtstart_line = f"DTSTART:{start_clean}"
        dtend_line = f"DTEND:{end_clean}"
        
    ics_content = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Voice Notes Pipeline//NONSGML//EN
BEGIN:VEVENT
UID:{uid}
DTSTAMP:{dtstamp}
SUMMARY:{title_clean}
{dtstart_line}
{dtend_line}
BEGIN:VALARM
TRIGGER:-PT15M
ACTION:DISPLAY
DESCRIPTION:Reminder: {title_clean}
END:VALARM
END:VEVENT
END:VCALENDAR"""

    url = f"{RADICALE_CALENDAR_URL.rstrip('/')}/{uid}.ics"
    if DRY_RUN:
        logging.info(f"[DRY RUN] Would push event '{title}' to Radicale: {url}")
        logging.info(f"[DRY RUN] Event payload:\n{ics_content}")
        return True
    try:
        r = requests.put(url, data=ics_content.encode('utf-8'), headers={'Content-Type': 'text/calendar; charset=utf-8'}, auth=RADICALE_AUTH, timeout=10)
        r.raise_for_status()
        logging.info(f"Successfully pushed event '{title}' to Radicale: {url}")
        return True
    except Exception as e:
        logging.error(f"Failed to push event to Radicale: {e}")
        return False

def push_task_to_radicale(title, due_date=None):
    uid = str(uuid.uuid4())
    dtstamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    title_clean = title.replace('"', '\\"').replace('\n', ' ')
    
    due_line = ""
    alarm_block = ""
    if due_date:
        date_clean = due_date.replace("-", "")
        due_line = f"DUE;VALUE=DATE:{date_clean}"
        alarm_block = f"""BEGIN:VALARM
TRIGGER;VALUE=DATE-TIME:{date_clean}T090000
ACTION:DISPLAY
DESCRIPTION:Task Reminder: {title_clean}
END:VALARM"""
        
    ics_content = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Voice Notes Pipeline//NONSGML//EN
BEGIN:VTODO
UID:{uid}
DTSTAMP:{dtstamp}
SUMMARY:{title_clean}
STATUS:NEEDS-ACTION
{due_line}
{alarm_block}
END:VTODO
END:VCALENDAR"""

    url = f"{RADICALE_TASKS_URL.rstrip('/')}/{uid}.ics"
    if DRY_RUN:
        logging.info(f"[DRY RUN] Would push task '{title}' (due: {due_date}) to Radicale: {url}")
        logging.info(f"[DRY RUN] Task payload:\n{ics_content}")
        return True
    try:
        r = requests.put(url, data=ics_content.encode('utf-8'), headers={'Content-Type': 'text/calendar; charset=utf-8'}, auth=RADICALE_AUTH, timeout=10)
        r.raise_for_status()
        logging.info(f"Successfully pushed task '{title}' to Radicale: {url}")
        return True
    except Exception as e:
        logging.error(f"Failed to push task to Radicale: {e}")
        return False

def write_to_inbox(original_filename, detected_lang, original_text, llm_content):
    categories = parse_categories_from_llm(llm_content)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base_name, _ = os.path.splitext(original_filename)
    output_filename = f"VoiceNote-{timestamp}.md"
    
    destinations = []
    for cat in categories:
        folder = cat.capitalize()
        target_dir = os.path.join(INBOX_DIR, folder)
        full_path = os.path.join(target_dir, output_filename)
        wiki_link = f"VoiceNotes/Inbox/{folder}/{output_filename[:-3]}"
        destinations.append({
            "category": cat,
            "full_path": full_path,
            "wiki_link": wiki_link,
            "display_path": f"Inbox/{folder}/{output_filename}"
        })
        
    for dest in destinations:
        os.makedirs(os.path.dirname(dest["full_path"]), exist_ok=True)
        
        # Parse existing frontmatter keys if present
        body = llm_content
        yaml_data = {}
        has_yaml = llm_content.strip().startswith("---")
        if has_yaml:
            parts = llm_content.split("---", 2)
            if len(parts) >= 3:
                for line in parts[1].splitlines():
                    line = line.strip()
                    if ":" in line and not line.startswith("-"):
                        k, v = line.split(":", 1)
                        yaml_data[k.strip()] = v.strip().replace('"', '').replace("'", "")
                body = parts[2]
                
        # Merge YAML keys
        yaml_data["categories"] = categories
        if "title" not in yaml_data:
            yaml_data["title"] = f"Voice Note: {base_name}"
        if "date_created" not in yaml_data:
            yaml_data["date_created"] = datetime.now().strftime('%Y-%m-%d')
        if "tags" not in yaml_data:
            yaml_data["tags"] = ["voicenote", "inbox"]
            
        # Add approval staging queue if it's an appointments note
        if "appointments" in categories:
            yaml_data["status"] = "pending"
            
        siblings = [d["display_path"] for d in destinations if d != dest]
        if siblings:
            yaml_data["copies"] = siblings
            
        # Rebuild YAML block
        yaml_lines = ["---"]
        for k, v in yaml_data.items():
            if k in ("categories", "copies", "tags"):
                yaml_lines.append(f"{k}:")
                list_items = categories if k == "categories" else (siblings if k == "copies" else ["voicenote", "inbox"])
                for item in list_items:
                    yaml_lines.append(f"  - {item}")
            else:
                yaml_lines.append(f"{k}: \"{v}\"")
        yaml_lines.append("---")
        yaml_str = "\n".join(yaml_lines) + "\n"
        
        note_content = yaml_str + body
        
        # Append cross-posting Obsidian links
        if siblings:
            note_content += "\n\n> [!NOTE] Cross-Posted\n> This note was also routed to:\n"
            for d in destinations:
                if d != dest:
                    note_content += f"> - [[{d['wiki_link']}|{d['display_path']}]]\n"
                    
        # Append processing metadata
        note_content += f"\n\n---\n## Harvester Metadata\n"
        note_content += f"- **Original Audio file:** `{original_filename}`\n"
        note_content += f"- **Detected Language:** `{detected_lang}`\n"
        note_content += f"- **Processed At:** `{datetime.now().isoformat()}`\n\n"
        note_content += f"### Original Raw Transcription\n"
        note_content += f"> {original_text}\n"
        
        with open(dest["full_path"], "w", encoding="utf-8") as f:
            f.write(note_content)
            
    logging.info(f"Saved note copies to {[d['display_path'] for d in destinations]} (Categories: {categories})")
    return [d["full_path"] for d in destinations]

def check_and_sync_approved_notes():
    """
    Recursively scans the INBOX_DIR for any .md files.
    If a file contains 'status: "approved"' in its frontmatter:
      - Parses the calendar event.
      - Parses the tasks.
      - Pushes them to Radicale.
      - Overwrites the file to change status from 'approved' to 'synced'.
    """
    if not os.path.exists(INBOX_DIR):
        return
        
    for root, dirs, files in os.walk(INBOX_DIR):
        for file in files:
            if file.endswith(".md"):
                filepath = os.path.join(root, file)
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        content = f.read()
                except Exception as e:
                    logging.error(f"Failed to read file {filepath} for status check: {e}")
                    continue
                    
                # Parse frontmatter to check status
                if not content.startswith("---"):
                    continue
                parts = content.split("---", 2)
                if len(parts) < 3:
                    continue
                frontmatter = parts[1]
                body = parts[2]
                
                is_approved = False
                fm_lines = []
                for line in frontmatter.splitlines():
                    clean_line = line.strip()
                    if clean_line.lower().startswith("status:"):
                        val = clean_line.split(":", 1)[1].strip().replace('"', '').replace("'", "").lower()
                        if val == "approved":
                            is_approved = True
                            # Change status to synced
                            fm_lines.append('status: "synced"')
                        else:
                            fm_lines.append(line)
                    else:
                        fm_lines.append(line)
                        
                if is_approved:
                    logging.info(f"Detected approved note for sync: {filepath}")
                    
                    # 1. Parse Event from Frontmatter
                    event = parse_event_from_frontmatter(content)
                    if event:
                        logging.info(f"Syncing event '{event['title']}' to Radicale...")
                        push_event_to_radicale(
                            title=event.get("title"),
                            date_str=event.get("date"),
                            start_time=event.get("startTime"),
                            end_time=event.get("endTime"),
                            all_day=event.get("allDay", True)
                        )
                        
                    # 2. Parse Tasks from Body
                    tasks = parse_tasks_from_markdown(content)
                    if tasks:
                        logging.info(f"Syncing {len(tasks)} tasks to Radicale...")
                        for task in tasks:
                            push_task_to_radicale(
                                title=task.get("title"),
                                due_date=task.get("due_date")
                            )
                            
                    # Update file with status: synced
                    new_frontmatter = "\n".join(fm_lines)
                    new_content = f"---{new_frontmatter}\n---{body}"
                    try:
                        with open(filepath, "w", encoding="utf-8") as f:
                            f.write(new_content)
                        logging.info(f"Successfully updated note status to 'synced' for: {filepath}")
                    except Exception as e:
                        logging.error(f"Failed to write updated status to {filepath}: {e}")

def archive_file(filepath):
    import shutil
    filename = os.path.basename(filepath)
    try:
        os.makedirs(ARCHIVE_DIR, exist_ok=True)
    except Exception as e:
        logging.error(f"Failed to create archive directory {ARCHIVE_DIR}: {e}")
        return False
        
    archive_path = os.path.join(ARCHIVE_DIR, filename)
    if os.path.exists(archive_path):
        base, ext = os.path.splitext(filename)
        archive_path = os.path.join(ARCHIVE_DIR, f"{base}_{int(time.time())}{ext}")
        
    try:
        shutil.move(filepath, archive_path)
        logging.info(f"Archived raw file: {filename} -> {archive_path}")
        return True
    except Exception as e:
        logging.error(f"Failed to move file to archive: {e}")
        return False

def process_file(filepath):
    filename = os.path.basename(filepath)
    raw_transcript, lang, prob = transcribe_audio(filepath)
    
    if not raw_transcript.strip():
        logging.warning(f"Audio file produced no transcript: {filename}. Skipping LLM.")
        return False
        
    llm_content = clean_and_extract_llm(raw_transcript)
    if not llm_content:
        logging.error(f"LLM processing failed for {filename}. Will retry on next check.")
        return False
        
    write_to_inbox(filename, f"{lang} ({prob:.2%})", raw_transcript, llm_content)
    
    # Archive the processed raw recording
    archive_file(filepath)
    return True

def monitor_loop():
    logging.info("Starting folder watch loop...")
    logging.info(f"Watching: {RAW_DIR}")
    logging.info(f"Writing to: {INBOX_DIR}")
    
    os.makedirs(RAW_DIR, exist_ok=True)
    os.makedirs(INBOX_DIR, exist_ok=True)
    
    while True:
        state = load_state()
        files = [f for f in os.listdir(RAW_DIR) if f.lower().endswith(SUPPORTED_EXTENSIONS)]
        for filename in files:
            filepath = os.path.join(RAW_DIR, filename)
            try:
                stat = os.stat(filepath)
                mtime = stat.st_mtime
                size = stat.st_size
            except OSError:
                continue
                
            if filename in state:
                if state[filename].get("size") == size and state[filename].get("mtime") == mtime:
                    continue
            
            logging.info(f"Detected new or modified file: {filename}")
            if not wait_for_file_to_stabilize(filepath):
                continue
                
            try:
                stat = os.stat(filepath)
                mtime = stat.st_mtime
                size = stat.st_size
            except OSError:
                continue
                
            success = process_file(filepath)
            if success:
                state[filename] = {
                    "size": size,
                    "mtime": mtime,
                    "processed_at": datetime.now().isoformat()
                }
                save_state(state)
                logging.info(f"Successfully processed and recorded state for: {filename}")
            else:
                logging.error(f"Failed processing file: {filename}. It will be retried.")
                
        # Run the approved notes check on every loop iteration
        check_and_sync_approved_notes()
        time.sleep(5)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Voice Notes Harvester Daemon")
    parser.add_argument("-d", "--dry-run", action="store_true", help="Run in dry-run mode (no external requests to LLM or CalDAV)")
    args = parser.parse_args()
    
    if args.dry_run:
        DRY_RUN = True
        logging.info("Running in DRY RUN mode. External API and CalDAV requests will be mocked.")
        
    try:
        monitor_loop()
    except KeyboardInterrupt:
        logging.info("Harvester terminated by user.")
        sys.exit(0)
