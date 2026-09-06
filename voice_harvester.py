#!/usr/bin/env python3
"""
Voice Notes Harvester & Thought Conveyor
Processes local multi-lingual voice notes on CPU, classifies and cleans them using a local LLM (Qwen 35B),
deposits formatted notes in categorized subfolders inside the Obsidian Vault Inbox.
If a note contains appointments/tasks, it sets 'status: pending' in the frontmatter.
When a user approves a note by setting 'status: approved', it automatically pushes the
finalized events and tasks to Google Calendar/Tasks while preserving the sovereign
Obsidian Vault Markdown note, and updates the status to 'synced'.
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

# BAMA Unified Inference Gateway Configuration (24/7 routing proxy on Jasper Lake)
BAMA_GATEWAY_URL = os.environ.get("BAMA_GATEWAY_URL", "http://192.168.1.37:8090/v1")


def get_gateway_url():
    """Resolves active BAMA Gateway URL (environment override, localhost, or Jasper Lake LXC 107)."""
    env_url = os.environ.get("BAMA_GATEWAY_URL")
    if env_url:
        return env_url.rstrip("/")
    for candidate in ("http://127.0.0.1:8090/v1", "http://192.168.1.37:8090/v1"):
        try:
            r = requests.get(f"{candidate}/health", timeout=0.3)
            if r.status_code == 200:
                return candidate
        except Exception:
            pass
    return "http://192.168.1.37:8090/v1"


# Cloud Failover Configuration ("qwen_cloud", "vertex", or "none")
CLOUD_FAILOVER_PROVIDER = os.environ.get("VOICE_CLOUD_FAILOVER", "qwen_cloud").lower()
BAI_API_URL = os.environ.get("BAI_API_URL", "https://api.b.ai/v1/chat/completions")
BAI_MODEL = os.environ.get("BAI_MODEL", "qwen3.8-flash")

# Whisper Config
WHISPER_MODEL_NAME = os.environ.get("WHISPER_MODEL", "large-v3-turbo")
WHISPER_THREADS = int(os.environ.get("WHISPER_THREADS", "4"))
GROQ_API_URL = os.environ.get("GROQ_API_URL", "https://api.groq.com/openai/v1/audio/transcriptions")
GROQ_WHISPER_MODEL = os.environ.get("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo")

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


_cached_groq_key = None


def get_groq_api_key():
    """Retrieve Groq API key from environment or local .env file (cached)."""
    global _cached_groq_key
    if _cached_groq_key:
        return _cached_groq_key

    key = os.environ.get("GROQ_API_KEY")
    if key:
        _cached_groq_key = key.strip()
        return _cached_groq_key

    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("GROQ_API_KEY="):
                        val = line.split("=", 1)[1].split("#")[0].strip().strip('"').strip("'")
                        if val:
                            _cached_groq_key = val
                            return _cached_groq_key
        except Exception as e:
            logging.debug(f"Error reading {env_path}: {e}")
    return None


def transcribe_audio_groq(filepath):
    """Tier-2 STT Failover: Transcribes audio file using Groq Cloud Whisper API."""
    api_key = get_groq_api_key()
    if not api_key:
        logging.warning("Groq API key not found in env (GROQ_API_KEY) or .env; skipping Groq STT failover.")
        return "", "unknown", 0.0

    try:
        file_size = os.path.getsize(filepath)
        if file_size < 1024:
            logging.warning(f"Audio file is too small ({file_size} bytes): {filepath}. Skipping Groq STT.")
            return "", "unknown", 0.0
    except OSError:
        return "", "unknown", 0.0

    headers = {
        "Authorization": f"Bearer {api_key}"
    }

    max_retries = 2
    for attempt in range(1, max_retries + 1):
        try:
            logging.info(f"Attempting Tier-2 Groq Whisper transcription (attempt {attempt}/{max_retries}) for: {filepath}")
            with open(filepath, "rb") as f:
                files = {
                    "file": (os.path.basename(filepath), f)
                }
                data = {
                    "model": GROQ_WHISPER_MODEL,
                    "response_format": "verbose_json"
                }
                resp = requests.post(GROQ_API_URL, headers=headers, files=files, data=data, timeout=25)
                if resp.status_code == 429:
                    logging.warning("Groq API rate limited (429). Retrying in 2s...")
                    time.sleep(2)
                    continue
                resp.raise_for_status()
                res_json = resp.json()

                raw_transcript = res_json.get("text", "").strip()
                detected_lang = res_json.get("language", "unknown")
                logging.info(f"Groq ASR complete. Detected language: {detected_lang}")
                return raw_transcript, detected_lang, 0.95
        except Exception as e:
            logging.error(f"Groq Whisper attempt {attempt} failed for {filepath}: {e}")
            if attempt < max_retries:
                time.sleep(2)

    return "", "unknown", 0.0


def transcribe_audio(filepath):
    """Transcribes audio file using BAMA Unified Gateway with local Faster-Whisper and Groq fallback."""
    raw_transcript = ""
    detected_lang = "unknown"
    confidence = 0.0

    # Tier 0: BAMA Unified Inference Gateway (Primary 24/7 cascading STT proxy)
    gateway_url = get_gateway_url()
    try:
        url = f"{gateway_url.rstrip('/')}/audio/transcriptions"
        logging.info(f"Transcribing audio via BAMA Gateway ({url}): {filepath}")
        with open(filepath, "rb") as f:
            files = {"file": (os.path.basename(filepath), f)}
            data = {"model": "whisper-1"}
            resp = requests.post(url, files=files, data=data, timeout=60)
            if resp.status_code == 200:
                res_json = resp.json()
                raw_transcript = res_json.get("text", "").strip()
                detected_lang = res_json.get("language", "unknown")
                confidence = 0.95
                tier_used = res_json.get("tier_used", "gateway")
                logging.info(f"BAMA Gateway ASR complete via {tier_used}. Language: {detected_lang}")
                if raw_transcript:
                    return raw_transcript, detected_lang, confidence
    except Exception as e:
        logging.warning(f"BAMA Gateway STT unavailable or failed ({e}). Attempting local fallback...")

    # Tier 1: Local Faster-Whisper
    try:
        model = load_whisper()
        if model:
            logging.info(f"Transcribing audio with local Whisper: {filepath}")
            segments, info = model.transcribe(filepath, beam_size=5)
            raw_text_parts = [segment.text for segment in segments]
            raw_transcript = " ".join(raw_text_parts).strip()
            detected_lang = info.language
            confidence = info.language_probability
            logging.info(f"Local ASR complete. Language detected: {detected_lang} ({confidence:.2f})")
            if raw_transcript:
                return raw_transcript, detected_lang, confidence
    except Exception as e:
        logging.warning(f"Local Whisper transcription failed for {filepath}: {e}")

    # Tier 2: Groq Cloud Whisper API Failover
    logging.warning(f"Initiating Tier-2 STT Cloud Failover to Groq Whisper for {filepath}...")
    raw_transcript, detected_lang, confidence = transcribe_audio_groq(filepath)
    if raw_transcript:
        return raw_transcript, detected_lang, confidence

    logging.error(f"All STT tiers failed for {filepath}.")
    return "", "unknown", 0.0


def clean_and_extract_llm(raw_text, max_retries=3, initial_backoff=2):
    """Sends transcript to BAMA Unified Inference Gateway for multi-tier cascading LLM distillation."""
    if DRY_RUN:
        logging.info(f"[DRY RUN] Simulating LLM request to {BAMA_GATEWAY_URL}...")
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
        "max_tokens": 1200,
        "stream": False
    }

    # Primary: BAMA Unified Inference Gateway
    gateway_url = get_gateway_url()
    backoff = initial_backoff
    for attempt in range(1, max_retries + 1):
        try:
            url = f"{gateway_url.rstrip('/')}/chat/completions"
            logging.info(f"Sending transcript to BAMA Gateway ({url}) (attempt {attempt}/{max_retries})...")
            response = requests.post(url, headers=headers, json=payload, timeout=60)
            if response.status_code == 200:
                res_json = response.json()
                llm_output = res_json['choices'][0]['message']['content']
                tier_used = res_json.get('_gateway_tier', 'gateway')
                logging.info(f"BAMA Gateway LLM response received successfully via {tier_used}.")
                return llm_output
        except Exception as e:
            logging.warning(f"BAMA Gateway LLM request attempt {attempt} failed: {e}")
            if attempt < max_retries:
                time.sleep(backoff)
                backoff *= 2

    # Direct local/cloud fallback if gateway fails
    logging.warning("BAMA Gateway unreachable. Cascading to direct LLM backends...")
    try:
        response = requests.post(LLM_API_URL, headers=headers, json=payload, timeout=30)
        if response.status_code == 200:
            res_json = response.json()
            llm_output = res_json['choices'][0]['message']['content']
            logging.info("Direct local LLM response received successfully.")
            return llm_output
    except Exception as e:
        logging.warning(f"Direct local LLM failed: {e}")

    # Tier-2/Tier-3 Failover Cascade
    if CLOUD_FAILOVER_PROVIDER == "qwen_cloud":
        logging.warning("All local LLM attempts failed. Initiating Tier-2 Cloud Failover to b.ai Qwen...")
        cloud_output = extract_llm_cloud_qwen(messages)
        if cloud_output:
            return cloud_output
        logging.warning("b.ai Qwen failover failed or unavailable. Cascading to Tier-3 Vertex AI Gemini Flash safety net...")
        cloud_output = extract_llm_vertex_gemini(messages)
        if cloud_output:
            return cloud_output
    elif CLOUD_FAILOVER_PROVIDER == "vertex":
        logging.warning("All local LLM attempts failed. Initiating Tier-2 Cloud Failover to Vertex AI Gemini Flash...")
        cloud_output = extract_llm_vertex_gemini(messages)
        if cloud_output:
            return cloud_output
    elif CLOUD_FAILOVER_PROVIDER == "none":
        logging.info("Cloud failover disabled by configuration (VOICE_CLOUD_FAILOVER=none).")

    logging.error("All configured LLM tiers failed.")
    return None


def get_bai_api_key():
    """Retrieve b.ai API key from environment or ~/.hermes/bai.env."""
    key = os.environ.get("B_API_KEY") or os.environ.get("BAI_API_KEY")
    if key:
        return key.strip()
    env_path = os.path.expanduser("~/.hermes/bai.env")
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("B_API_KEY="):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
                    if line.startswith("CUSTOM_API_KEY="):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
        except Exception as e:
            logging.debug(f"Error reading ~/.hermes/bai.env: {e}")
    return None


def extract_llm_cloud_qwen(messages):
    """Tier-2 Cloud Failover: calls Qwen 3.8 Flash via b.ai custom API."""
    api_key = get_bai_api_key()
    if not api_key:
        logging.warning("b.ai API key not found in env (B_API_KEY) or ~/.hermes/bai.env; skipping cloud Qwen.")
        return None

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": BAI_MODEL,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": 1200,
        "chat_template_kwargs": {"reasoning_effort": "low"},
        "stream": False
    }
    try:
        logging.info(f"Requesting completion from b.ai Cloud Qwen ({BAI_MODEL})...")
        resp = requests.post(BAI_API_URL, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        res_json = resp.json()
        content = res_json['choices'][0]['message']['content']
        logging.info(f"Tier-2 b.ai Cloud Qwen ({BAI_MODEL}) response received successfully.")
        return content
    except Exception as e:
        logging.error(f"Tier-2 b.ai Cloud Qwen failover failed: {e}")
        return None


def extract_llm_vertex_gemini(messages):
    """Tier-3 Cloud Failover: calls Gemini 3.7 Flash via Vertex AI."""
    try:
        import google.auth
        from google.auth.transport.requests import Request
        project_id = os.environ.get("VERTEX_PROJECT_ID", "project-a4472335-7c7d-4369-8b4")
        creds, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
        creds.refresh(Request())

        url = f"https://aiplatform.googleapis.com/v1beta1/projects/{project_id}/locations/global/endpoints/openapi/chat/completions"
        headers = {
            "Authorization": f"Bearer {creds.token}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": "google/gemini-3.7-flash",
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": 1500
        }
        logging.info("Requesting completion from Vertex AI Gemini 3.7 Flash...")
        resp = requests.post(url, headers=headers, json=payload, timeout=20)
        resp.raise_for_status()
        res_json = resp.json()
        content = res_json['choices'][0]['message']['content']
        logging.info("Tier-2 Vertex AI Gemini 3.7 Flash response received successfully.")
        return content
    except Exception as e:
        logging.error(f"Tier-2 Vertex AI Gemini 3.7 Flash failover failed: {e}")
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
            # Google Calendar all-day events require an exclusive end date (date + 1 day)
            start_body = {'date': date_str}
            try:
                next_day = (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
            except Exception:
                next_day = date_str
            end_body = {'date': next_day}
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
    """Syncs event to Google Calendar (Primary). Obsidian Markdown remains the local canonical record."""
    title = event_dict.get("title")
    date_str = event_dict.get("date")
    start_time = event_dict.get("startTime")
    end_time = event_dict.get("endTime")
    all_day = event_dict.get("allDay", True)

    return push_event_to_gcal(title, date_str, start_time, end_time, all_day)


def sync_task(task_dict):
    """Syncs task to Google Tasks (Primary). Obsidian Markdown remains the local canonical record."""
    title = task_dict.get("title")
    due_date = task_dict.get("due_date")

    return push_task_to_gtasks(title, due_date)


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
        logging.warning(f"All LLM tiers failed for {filename}. Triggering Tier-3 Dead-Letter fallback.")
        dead_letter_content = (
            f"---\n"
            f"categories:\n"
            f"  - life\n"
            f"title: \"Voice Note: {os.path.splitext(filename)[0]}\"\n"
            f"status: dead-letter\n"
            f"tags:\n"
            f"  - voicenote\n"
            f"  - inbox\n"
            f"  - dead-letter\n"
            f"  - unprocessed-llm\n"
            f"---\n\n"
            f"# Raw Transcript (LLM Structuring Unavailable)\n\n"
            f"{raw_transcript}\n"
        )
        write_to_inbox(filename, f"{lang} ({prob:.2%})", raw_transcript, dead_letter_content)
        archive_file(filepath)
        return "dead_letter"

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
                retry_count = state.get(filename, {}).get("retries", 0) + 1
                state[filename] = {
                    "size": size,
                    "mtime": mtime,
                    "retries": retry_count,
                    "last_attempt": datetime.now().isoformat()
                }
                if retry_count >= 3:
                    logging.error(f"Max retries (3) reached for {filename}. Moving to dead-letter archive.")
                    archive_file(filepath)
                    state[filename]["status"] = "dead_letter"
                else:
                    logging.warning(f"Processing attempt {retry_count}/3 failed for {filename}. Will retry.")
                save_state(state)

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
