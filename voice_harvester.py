#!/usr/bin/env python3
"""
Voice Notes Harvester & Thought Conveyor
Processes local multi-lingual voice notes on CPU, classifies and cleans them using a local LLM (Qwen 35B),
deposits formatted notes in categorized subfolders inside the Obsidian Vault Inbox.
If a note contains appointments/tasks, it sets 'status: pending' in the frontmatter.
When a user approves a note by setting 'status: approved', it automatically pushes the
finalized events and tasks to Google Calendar/Tasks (primary) or Radicale CalDAV (fallback)
and updates the status to 'synced'.
"""

import os
import sys
import time
import json
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta
import requests
import uuid
import re
import yaml
from filelock import FileLock

# Configure Logging with Rotation
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "harvester.log")
log_handler = RotatingFileHandler(LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
log_handler.setFormatter(log_formatter)

logger = logging.getLogger()
logger.setLevel(logging.INFO)
if not logger.handlers:
    logger.addHandler(logging.StreamHandler(sys.stdout))
    logger.addHandler(log_handler)

# Configuration Defaults (Overrides via environment variables)
RAW_DIR = os.environ.get("VOICE_RAW_DIR", "/home/fuad/Seafile/Obsidian Vaults/VoiceNotes/Raw/")
INBOX_DIR = os.environ.get("VOICE_INBOX_DIR", "/home/fuad/Seafile/Obsidian Vaults/VoiceNotes/Inbox/")
STATE_FILE = os.environ.get("VOICE_STATE_FILE", "/home/fuad/Seafile/Obsidian Vaults/VoiceNotes/processed_files.json")
DEFAULT_SYSTEM_PROMPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "system_prompt.md")
SYSTEM_PROMPT_PATH = os.environ.get("VOICE_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT)
ARCHIVE_DIR = os.environ.get("VOICE_ARCHIVE_DIR", "/mnt/RAID5/VoiceNotesArchive/")

LLM_API_URL = os.environ.get("LLM_API_URL", "http://127.0.0.1:1235/v1/chat/completions")  # Port 1235 maps to Qwen 35B
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen")

# Whisper Config
WHISPER_MODEL_NAME = os.environ.get("WHISPER_MODEL", "large-v3-turbo")
WHISPER_THREADS = int(os.environ.get("WHISPER_THREADS", "4"))

# Radicale CalDAV Configuration (Fallback)
RADICALE_CALENDAR_URL = os.environ.get("RADICALE_CALENDAR_URL", "http://192.168.1.30:5232/fuad/64e71687-ed01-f827-c34f-38222fd871f5/")
RADICALE_TASKS_URL = os.environ.get("RADICALE_TASKS_URL", "http://192.168.1.30:5232/fuad/a88e07f8-1c04-17c6-6a6c-5be1c5bf0879/")
RADICALE_USER = os.environ.get("RADICALE_USER", "fuad")
RADICALE_PASSWORD = os.environ.get("RADICALE_PASSWORD", "")
RADICALE_AUTH = (RADICALE_USER, RADICALE_PASSWORD) if RADICALE_PASSWORD else None

# Google Calendar & Tasks Configuration (Primary)
DEFAULT_GCAL_CREDS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gcal_credentials.json")
DEFAULT_GTASKS_TOKEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "token.json")
GCAL_CREDENTIALS_FILE = os.environ.get("GCAL_CREDENTIALS", DEFAULT_GCAL_CREDS)
GCAL_CALENDAR_ID = os.environ.get("GCAL_CALENDAR_ID", "fuad.babaev@gmail.com")
GTASKS_TOKEN_FILE = os.environ.get("GTASKS_TOKEN_FILE", DEFAULT_GTASKS_TOKEN)

# Supported Extensions
SUPPORTED_EXTENSIONS = (".mp3", ".wav", ".m4a", ".ogg", ".aac", ".opus")

# Global Whisper Model Instance
whisper_model = None

# Dry run mode flag
DRY_RUN = False


def atomic_write(filepath, content, encoding="utf-8"):
    """Writes content to a temporary file in the same directory and replaces atomically."""
    os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
    tmp_path = f"{filepath}.tmp.{uuid.uuid4().hex}"
    try:
        with open(tmp_path, "w", encoding=encoding) as f:
            f.write(content)
        os.replace(tmp_path, filepath)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise


def load_whisper():
    """Loads faster-whisper model lazily without crashing the daemon on failure."""
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
        except ImportError as e:
            logging.error("faster-whisper is not installed. Run: pip install faster-whisper")
            raise e
        except Exception as e:
            logging.error(f"Failed to load Whisper model: {e}")
            raise e
    return whisper_model


def load_state():
    """Loads processed files state safely."""
    if os.path.exists(STATE_FILE):
        try:
            lock_path = f"{STATE_FILE}.lock"
            with FileLock(lock_path, timeout=5):
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            logging.warning(f"State file load warning ({e}). Initializing empty state.")
            return {}
    return {}


def save_state(state):
    """Saves processed files state atomically under lock."""
    os.makedirs(os.path.dirname(os.path.abspath(STATE_FILE)), exist_ok=True)
    lock_path = f"{STATE_FILE}.lock"
    with FileLock(lock_path, timeout=5):
        tmp_path = f"{STATE_FILE}.tmp.{uuid.uuid4().hex}"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=4, ensure_ascii=False)
            os.replace(tmp_path, STATE_FILE)
        except Exception:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            raise


def wait_for_file_to_stabilize(filepath, check_interval=2, timeout=60):
    """Waits for file size to stabilize before processing."""
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
    """Transcribes audio file using Whisper."""
    try:
        model = load_whisper()
    except Exception as e:
        logging.error(f"Cannot transcribe {filepath}: Whisper model not available ({e})")
        return "", "unknown", 0.0

    logging.info(f"Transcribing audio: {filepath}")
    try:
        segments, info = model.transcribe(filepath, beam_size=5)
        raw_text_parts = [segment.text for segment in segments]
        raw_transcript = " ".join(raw_text_parts).strip()
        logging.info(f"ASR complete. Language detected: {info.language} ({info.language_probability:.2f})")
        return raw_transcript, info.language, info.language_probability
    except Exception as e:
        logging.error(f"Transcription error for {filepath}: {e}")
        return "", "unknown", 0.0


def clean_and_extract_llm(raw_text, max_retries=3, initial_backoff=2):
    """Sends transcript to local Qwen 35B LLM with exponential backoff."""
    if DRY_RUN:
        logging.info(f"[DRY RUN] Simulating LLM request to {LLM_API_URL}...")
        today_str = datetime.now().strftime("%Y-%m-%d")
        categories = ["life"]
        raw_lower = raw_text.lower()
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

        if len(categories) > 1 and "life" in categories:
            categories.remove("life")

        fm = {"categories": categories}
        if "appointments" in categories:
            fm.update({
                "title": "Mock Event",
                "allDay": False,
                "date": today_str,
                "startTime": "10:00",
                "endTime": "11:00"
            })

        body_parts = []
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

        fm_yaml = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)
        return f"---\n{fm_yaml}---\n" + "\n".join(body_parts)

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

    backoff = initial_backoff
    for attempt in range(1, max_retries + 1):
        try:
            logging.info(f"Sending transcript to local LLM at {LLM_API_URL} (attempt {attempt}/{max_retries})...")
            response = requests.post(LLM_API_URL, headers=headers, json=payload, timeout=120)
            response.raise_for_status()
            res_json = response.json()
            llm_output = res_json['choices'][0]['message']['content']
            logging.info("LLM response received successfully.")
            return llm_output
        except Exception as e:
            logging.warning(f"LLM request attempt {attempt} failed: {e}")
            if attempt < max_retries:
                time.sleep(backoff)
                backoff *= 2
            else:
                logging.error(f"All LLM retry attempts failed: {e}")
                return None


def extract_frontmatter_and_body(markdown_text):
    """Robustly extracts YAML frontmatter dictionary and Markdown body using PyYAML."""
    content = markdown_text.strip()
    if not content.startswith("---"):
        return {}, content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content
    raw_fm = parts[1]
    body = parts[2]
    try:
        data = yaml.safe_load(raw_fm)
        if isinstance(data, dict):
            return data, body
        return {}, body
    except Exception as e:
        logging.warning(f"Failed to parse YAML frontmatter: {e}")
        return {}, body


def parse_categories_from_llm(llm_content):
    """Extracts unique category list from LLM output."""
    fm, _ = extract_frontmatter_and_body(llm_content)
    raw_cats = fm.get("categories", [])
    if isinstance(raw_cats, str):
        raw_cats = [raw_cats]
    elif not isinstance(raw_cats, list):
        raw_cats = []

    valid_cats = []
    for c in raw_cats:
        if isinstance(c, str):
            c_clean = c.strip().lower()
            if c_clean in ("appointments", "technical", "life") and c_clean not in valid_cats:
                valid_cats.append(c_clean)

    return valid_cats if valid_cats else ["life"]


def parse_event_from_frontmatter(content):
    """Parses appointment/event fields from note frontmatter."""
    fm, _ = extract_frontmatter_and_body(content)
    if not fm:
        return None

    # Case-insensitive lookup helper
    fm_norm = {str(k).lower(): v for k, v in fm.items()}
    title = fm_norm.get("title")
    date_val = fm_norm.get("date")

    if not title or not date_val:
        return None

    start_time = fm_norm.get("starttime") or fm_norm.get("start_time")
    end_time = fm_norm.get("endtime") or fm_norm.get("end_time")
    all_day = fm_norm.get("allday") or fm_norm.get("all_day")

    if all_day is None:
        all_day = (start_time is None)
    elif isinstance(all_day, str):
        all_day = (all_day.lower() == "true")
    else:
        all_day = bool(all_day)

    return {
        "title": str(title).strip(),
        "date": str(date_val).strip(),
        "startTime": str(start_time).strip() if start_time else None,
        "endTime": str(end_time).strip() if end_time else None,
        "allDay": all_day
    }


def parse_tasks_from_markdown(content):
    """Extracts todo checkbox tasks from markdown text."""
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


def normalize_time_str(time_str):
    """Normalizes time string (e.g. '9:5' -> '09:05:00')."""
    if not time_str:
        return None
    time_str = time_str.strip()
    match = re.match(r"^(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?$", time_str)
    if match:
        h, m, s = match.groups()
        s = s or "00"
        return f"{int(h):02d}:{int(m):02d}:{int(s):02d}"
    return time_str


def push_event_to_radicale(title, date_str, start_time=None, end_time=None, all_day=True):
    """Pushes event to Radicale CalDAV server as fallback."""
    if not RADICALE_AUTH:
        logging.info("Radicale credentials not configured. Skipping Radicale push.")
        return False

    uid = str(uuid.uuid4())
    dtstamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    title_clean = title.replace('"', '\\"').replace('\n', ' ')

    if all_day or not start_time:
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
        norm_start = normalize_time_str(start_time)
        norm_end = normalize_time_str(end_time) if end_time else None

        start_clean = f"{date_str.replace('-', '')}T{norm_start.replace(':', '')}"
        if norm_end:
            end_clean = f"{date_str.replace('-', '')}T{norm_end.replace(':', '')}"
        else:
            try:
                s_dt = datetime.strptime(f"{date_str} {norm_start[:5]}", "%Y-%m-%d %H:%M")
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
        return True

    try:
        r = requests.put(
            url,
            data=ics_content.encode('utf-8'),
            headers={'Content-Type': 'text/calendar; charset=utf-8'},
            auth=RADICALE_AUTH,
            timeout=10
        )
        r.raise_for_status()
        logging.info(f"Successfully pushed event '{title}' to Radicale CalDAV: {url}")
        return True
    except Exception as e:
        logging.error(f"Failed to push event to Radicale: {e}")
        return False


def push_task_to_radicale(title, due_date=None):
    """Pushes todo task to Radicale CalDAV server as fallback."""
    if not RADICALE_AUTH:
        logging.info("Radicale credentials not configured. Skipping Radicale task push.")
        return False

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
        return True

    try:
        r = requests.put(
            url,
            data=ics_content.encode('utf-8'),
            headers={'Content-Type': 'text/calendar; charset=utf-8'},
            auth=RADICALE_AUTH,
            timeout=10
        )
        r.raise_for_status()
        logging.info(f"Successfully pushed task '{title}' to Radicale CalDAV: {url}")
        return True
    except Exception as e:
        logging.error(f"Failed to push task to Radicale: {e}")
        return False


def push_event_to_gcal(title, date_str, start_time=None, end_time=None, all_day=True):
    """Pushes event to Google Calendar (Primary)."""
    if not os.path.exists(GCAL_CREDENTIALS_FILE):
        logging.info(f"Google Calendar credentials file ({GCAL_CREDENTIALS_FILE}) not found. Skipping GCal push.")
        return False

    if DRY_RUN:
        logging.info(f"[DRY RUN] Would push event '{title}' ({date_str} {start_time}) to Google Calendar '{GCAL_CALENDAR_ID}'.")
        return True

    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        SCOPES = ['https://www.googleapis.com/auth/calendar']
        creds = service_account.Credentials.from_service_account_file(GCAL_CREDENTIALS_FILE, scopes=SCOPES)
        service = build('calendar', 'v3', credentials=creds)

        if all_day or not start_time:
            start_body = {'date': date_str}
            end_body = {'date': date_str}
        else:
            norm_start = normalize_time_str(start_time)[:5]
            start_iso = f"{date_str}T{norm_start}:00"
            if end_time:
                norm_end = normalize_time_str(end_time)[:5]
            else:
                try:
                    h, m = norm_start.split(":")
                    norm_end = f"{(int(h)+1)%24:02d}:{m}"
                except Exception:
                    norm_end = norm_start
            end_iso = f"{date_str}T{norm_end}:00"
            start_body = {'dateTime': start_iso, 'timeZone': 'Asia/Baku'}
            end_body = {'dateTime': end_iso, 'timeZone': 'Asia/Baku'}

        event_body = {
            'summary': title,
            'start': start_body,
            'end': end_body,
            'reminders': {'useDefault': True}
        }

        res = service.events().insert(calendarId=GCAL_CALENDAR_ID, body=event_body).execute()
        logging.info(f"Successfully pushed event '{title}' to Google Calendar ({GCAL_CALENDAR_ID}): {res.get('htmlLink')}")
        return True
    except Exception as e:
        logging.error(f"Failed to push event to Google Calendar: {e}")
        return False


def push_task_to_gtasks(title, due_date=None):
    """Pushes task to Google Tasks (Primary)."""
    if DRY_RUN:
        logging.info(f"[DRY RUN] Would push task '{title}' (due: {due_date}) to Google Tasks.")
        return True

    try:
        from googleapiclient.discovery import build
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request

        SCOPES = ['https://www.googleapis.com/auth/tasks']
        creds = None

        if os.path.exists(GTASKS_TOKEN_FILE):
            try:
                creds = Credentials.from_authorized_user_file(GTASKS_TOKEN_FILE, SCOPES)
                if creds and creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                    with open(GTASKS_TOKEN_FILE, 'w') as token:
                        token.write(creds.to_json())
                    os.chmod(GTASKS_TOKEN_FILE, 0o600)
            except Exception as e:
                logging.warning(f"Failed to load/refresh Google Tasks token.json: {e}")
                creds = None

        if not creds or not creds.valid:
            if os.path.exists(GCAL_CREDENTIALS_FILE):
                from google.oauth2 import service_account
                creds = service_account.Credentials.from_service_account_file(GCAL_CREDENTIALS_FILE, scopes=SCOPES)
            else:
                logging.info("Neither Google Tasks token.json nor service account credentials file found. Skipping Google Tasks push.")
                return False

        service = build('tasks', 'v1', credentials=creds)
        task_body = {'title': title}
        if due_date:
            try:
                task_body['due'] = f"{due_date}T09:00:00.000Z"
            except Exception:
                pass

        res = service.tasks().insert(tasklist='@default', body=task_body).execute()
        logging.info(f"Successfully pushed task '{title}' to Google Tasks: ID '{res.get('id')}'")
        return True
    except Exception as e:
        logging.error(f"Failed to push task to Google Tasks: {e}")
        return False


def sync_event(event_dict):
    """Syncs event to Google Calendar (Primary) with Radicale CalDAV fallback."""
    title = event_dict.get("title")
    date_str = event_dict.get("date")
    start_time = event_dict.get("startTime")
    end_time = event_dict.get("endTime")
    all_day = event_dict.get("allDay", True)

    gcal_ok = push_event_to_gcal(title, date_str, start_time, end_time, all_day)
    if gcal_ok:
        return True

    logging.info("GCal push unavailable or failed; attempting fallback to Radicale CalDAV...")
    radicale_ok = push_event_to_radicale(title, date_str, start_time, end_time, all_day)
    return radicale_ok


def sync_task(task_dict):
    """Syncs task to Google Tasks (Primary) with Radicale CalDAV fallback."""
    title = task_dict.get("title")
    due_date = task_dict.get("due_date")

    gtasks_ok = push_task_to_gtasks(title, due_date)
    if gtasks_ok:
        return True

    logging.info("Google Tasks push unavailable or failed; attempting fallback to Radicale CalDAV...")
    radicale_ok = push_task_to_radicale(title, due_date)
    return radicale_ok


def write_to_inbox(original_filename, detected_lang, original_text, llm_content):
    """Parses LLM output, applies frontmatter schema, and writes note files atomically."""
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

    existing_fm, body = extract_frontmatter_and_body(llm_content)

    for dest in destinations:
        yaml_data = dict(existing_fm)
        yaml_data["categories"] = categories
        if "title" not in yaml_data:
            yaml_data["title"] = f"Voice Note: {base_name}"
        if "date_created" not in yaml_data:
            yaml_data["date_created"] = datetime.now().strftime('%Y-%m-%d')
        if "tags" not in yaml_data:
            yaml_data["tags"] = ["voicenote", "inbox"]

        if "appointments" in categories:
            yaml_data["status"] = "pending"

        siblings = [d["display_path"] for d in destinations if d != dest]
        if siblings:
            yaml_data["copies"] = siblings

        # Build clean YAML frontmatter
        yaml_str = yaml.safe_dump(yaml_data, sort_keys=False, allow_unicode=True)
        note_content = f"---\n{yaml_str}---\n{body.lstrip()}"

        if siblings:
            note_content += "\n\n> [!NOTE] Cross-Posted\n> This note was also routed to:\n"
            for d in destinations:
                if d != dest:
                    note_content += f"> - [[{d['wiki_link']}|{d['display_path']}]]\n"

        note_content += f"\n\n---\n## Harvester Metadata\n"
        note_content += f"- **Original Audio file:** `{original_filename}`\n"
        note_content += f"- **Detected Language:** `{detected_lang}`\n"
        note_content += f"- **Processed At:** `{datetime.now().isoformat()}`\n\n"
        note_content += f"### Original Raw Transcription\n"
        note_content += f"> {original_text}\n"

        lock_path = f"{dest['full_path']}.lock"
        with FileLock(lock_path, timeout=10):
            atomic_write(dest["full_path"], note_content)

    logging.info(f"Saved note copies to {[d['display_path'] for d in destinations]} (Categories: {categories})")
    return [d["full_path"] for d in destinations]


def check_and_sync_approved_notes():
    """
    Scans INBOX_DIR for notes with 'status: approved', syncs events & tasks,
    and atomically updates status to 'synced' ONLY upon verified sync success.
    """
    if not os.path.exists(INBOX_DIR):
        return

    for root, _, files in os.walk(INBOX_DIR):
        for file in files:
            if not file.endswith(".md"):
                continue

            filepath = os.path.join(root, file)
            lock_path = f"{filepath}.lock"

            try:
                with FileLock(lock_path, timeout=5):
                    with open(filepath, "r", encoding="utf-8") as f:
                        content = f.read()

                    fm, body = extract_frontmatter_and_body(content)
                    status = str(fm.get("status", "")).strip().lower()

                    if status != "approved":
                        continue

                    logging.info(f"Detected approved note for sync: {filepath}")
                    sync_succeeded = True

                    # 1. Sync Event
                    event = parse_event_from_frontmatter(content)
                    if event:
                        logging.info(f"Syncing event '{event['title']}'...")
                        event_ok = sync_event(event)
                        if not event_ok:
                            sync_succeeded = False
                            logging.error(f"Event sync failed for note: {filepath}")

                    # 2. Sync Tasks
                    tasks = parse_tasks_from_markdown(content)
                    if tasks:
                        logging.info(f"Syncing {len(tasks)} tasks...")
                        for task in tasks:
                            task_ok = sync_task(task)
                            if not task_ok:
                                sync_succeeded = False
                                logging.error(f"Task sync failed for '{task.get('title')}' in {filepath}")

                    # 3. Only update to 'synced' if sync operations succeeded
                    if sync_succeeded:
                        fm["status"] = "synced"
                        new_yaml = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)
                        new_content = f"---\n{new_yaml}---\n{body.lstrip()}"
                        atomic_write(filepath, new_content)
                        logging.info(f"Successfully updated note status to 'synced' for: {filepath}")
                    else:
                        logging.warning(f"Note sync incomplete. Status remains 'approved' for retry: {filepath}")

            except Exception as e:
                logging.error(f"Error checking/syncing note {filepath}: {e}")


def archive_file(filepath):
    """Moves processed raw audio to archive directory safely."""
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
    """Processes a single raw audio recording."""
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
    archive_file(filepath)
    return True


def monitor_loop():
    """Main background loop watching RAW_DIR and checking approved notes."""
    logging.info("Starting folder watch loop...")
    logging.info(f"Watching: {RAW_DIR}")
    logging.info(f"Writing to: {INBOX_DIR}")

    os.makedirs(RAW_DIR, exist_ok=True)
    os.makedirs(INBOX_DIR, exist_ok=True)

    while True:
        state = load_state()
        try:
            files = [f for f in os.listdir(RAW_DIR) if f.lower().endswith(SUPPORTED_EXTENSIONS)]
        except Exception as e:
            logging.error(f"Error reading RAW_DIR: {e}")
            files = []

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
