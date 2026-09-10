#!/usr/bin/env python3
"""
Signal Voice Note Ingestion & Interactive Command Module for Voice Notes Pipeline
Connects to local signal-cli-rest-api via WebSocket:
1. Ingests incoming voice notes (Direct Message / Note-to-Self) asynchronously without blocking the event loop.
2. Sends instant receipt & completion alerts back to Signal.
3. Stages appointments and tasks with sovereign Obsidian Vault archiving and Google Calendar sync.
4. Handles text replies ('approve', 'reject', '15:30') with atomic frontmatter updates.
"""

import os
import sys
import time
import json
import re
import logging
from logging.handlers import RotatingFileHandler
import asyncio
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse
import requests
import websockets
import yaml
from filelock import FileLock
import voice_harvester
import seafile_sync

seafile_client = seafile_sync.default_client

# Configure Logging with Rotation
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "harvester.log")
log_handler = RotatingFileHandler(LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] [SIGNAL-INGEST] %(message)s")
log_handler.setFormatter(log_formatter)

logger = logging.getLogger()
logger.setLevel(logging.INFO)
if not logger.handlers:
    logger.addHandler(logging.StreamHandler(sys.stdout))
    logger.addHandler(log_handler)

# Configuration Defaults (Overrides via environment variables)
SIGNAL_API_URL = os.environ.get("SIGNAL_API_URL", "http://127.0.0.1:8080")
SIGNAL_PHONE_NUMBER = os.environ.get("SIGNAL_PHONE_NUMBER", "+994502214707")
STAGING_DIR = os.environ.get("VOICE_STAGING_DIR", "/tmp/signal_voice_staging/")
INBOX_DIR = os.environ.get("VOICE_INBOX_DIR", "/home/fuad/Seafile/Obsidian Vaults/VoiceNotes/Inbox/")

# Thread pool for offloading synchronous CPU / I/O tasks
worker_pool = ThreadPoolExecutor(max_workers=4)


def ensure_staging_dir():
    os.makedirs(STAGING_DIR, exist_ok=True)


def send_signal_message(recipient, message, group_id=None):
    """Sends a Signal text message via signal-cli REST API."""
    if not SIGNAL_PHONE_NUMBER:
        logging.warning("SIGNAL_PHONE_NUMBER not set. Skipping response dispatch.")
        return False

    url = f"{SIGNAL_API_URL.rstrip('/')}/v2/send"
    target = recipient or SIGNAL_PHONE_NUMBER
    payload = {
        "number": SIGNAL_PHONE_NUMBER,
        "recipients": [target],
        "message": message
    }

    try:
        r = requests.post(url, json=payload, timeout=15)
        r.raise_for_status()
        logging.info(f"Sent Signal response to {target}")
        return True
    except Exception as e:
        logging.error(f"Failed to send Signal message: {e}")
        return False


def download_attachment(attachment_id):
    """Downloads audio attachment from signal-cli-rest-api."""
    ensure_staging_dir()
    url = f"{SIGNAL_API_URL.rstrip('/')}/v1/attachments/{attachment_id}"
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        filename = f"signal_voice_{int(time.time())}_{attachment_id[:8]}.ogg"
        filepath = os.path.join(STAGING_DIR, filename)
        with open(filepath, "wb") as f:
            f.write(r.content)
        logging.info(f"Downloaded Signal attachment {attachment_id} to {filepath}")
        return filepath
    except Exception as e:
        logging.error(f"Failed to download attachment {attachment_id}: {e}")
        return None


def check_calendar_conflicts(date_str, start_time_str):
    """Performs conflict check against Google Calendar if available, otherwise reports staged."""
    if not date_str or date_str == "Today":
        return "🟢 **Calendar Free**"

    if os.path.exists(voice_harvester.GCAL_CREDENTIALS_FILE):
        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build

            SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']
            creds = service_account.Credentials.from_service_account_file(
                voice_harvester.GCAL_CREDENTIALS_FILE, scopes=SCOPES
            )
            service = build('calendar', 'v3', credentials=creds)

            time_min = f"{date_str}T00:00:00Z"
            time_max = f"{date_str}T23:59:59Z"
            events_result = service.events().list(
                calendarId=voice_harvester.GCAL_CALENDAR_ID,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy='startTime'
            ).execute()
            items = events_result.get('items', [])
            if items:
                conflicts = [item.get('summary', 'Existing Event') for item in items]
                return f"⚠️ **Calendar Conflict:** Existing entry on {date_str}: '{conflicts[0]}'"
            return "🟢 **Calendar Free** (No conflicts found)"
        except Exception as e:
            logging.debug(f"Google Calendar conflict check omitted: {e}")
            return "🟢 **Staged to Vault**"

    return "🟢 **Staged to Vault**"


def parse_note_details(content):
    """Parses title, date, and startTime from note content using PyYAML."""
    fm, _ = voice_harvester.extract_frontmatter_and_body(content)
    fm_norm = {str(k).lower(): v for k, v in fm.items()}

    title = fm_norm.get("title", "Appointment")
    date_val = fm_norm.get("date", "Today")
    time_val = fm_norm.get("starttime") or fm_norm.get("start_time") or "TBD"

    return str(title), str(date_val), str(time_val)


def get_pending_appointment_notes():
    """Scans INBOX_DIR for notes with pending status specifically in appointments category."""
    if not os.path.exists(INBOX_DIR):
        return []
    pending_notes = []
    for root, _, files in os.walk(INBOX_DIR):
        for file in files:
            if file.endswith(".md"):
                full_path = os.path.join(root, file)
                try:
                    with open(full_path, "r", encoding="utf-8") as f:
                        content = f.read()
                    fm, _ = voice_harvester.extract_frontmatter_and_body(content)
                    cats = fm.get("categories", [])
                    if isinstance(cats, str):
                        cats = [cats]
                    if "appointments" in cats and str(fm.get("status", "")).strip().lower() == "pending":
                        mtime = os.path.getmtime(full_path)
                        pending_notes.append((mtime, full_path, content))
                except Exception:
                    pass
    pending_notes.sort(key=lambda x: x[0], reverse=True)
    return pending_notes


def get_target_pending_agent_note():
    """Finds latest pending agent note in AgentBacklog folder."""
    agent_dir = os.path.join(INBOX_DIR, "AgentBacklog")
    if not os.path.exists(agent_dir):
        return None
    agent_notes = []
    for file in os.listdir(agent_dir):
        if file.endswith(".md"):
            full_path = os.path.join(agent_dir, file)
            try:
                with open(full_path, "r", encoding="utf-8") as f:
                    content = f.read()
                fm, _ = voice_harvester.extract_frontmatter_and_body(content)
                if str(fm.get("status", "")).strip().lower() == "pending":
                    mtime = os.path.getmtime(full_path)
                    agent_notes.append((mtime, full_path, content))
            except Exception:
                pass
    if not agent_notes:
        return None
    agent_notes.sort(key=lambda x: x[0], reverse=True)
    return agent_notes[0]


def parse_agent_note_details(content):
    """Extracts target and action from agent note body."""
    target = "General Homelab"
    action = "Agent task recorded"
    lines = content.splitlines()
    for i, line in enumerate(lines):
        line_s = line.strip()
        if line_s.startswith("# Target / Context") and i + 1 < len(lines):
            for next_line in lines[i+1:]:
                nl_s = next_line.strip()
                if nl_s.startswith("#"):
                    break
                if nl_s:
                    target = nl_s
                    break
        elif line_s.startswith("# Requested Agent Action") and i + 1 < len(lines):
            for next_line in lines[i+1:]:
                nl_s = next_line.strip()
                if nl_s.startswith("#"):
                    break
                if nl_s:
                    action = nl_s
                    break
    return target, action


def parse_spoken_time(text):
    """Parses time from spoken text (e.g. '15:30', '3pm', '3:30 pm')."""
    match = re.search(r'\b([0-1]?[0-9]|2[0-3]):([0-5][0-9])\b', text)
    if match:
        return match.group(0)
    match_ampm = re.search(r'\b(1[0-2]|[1-9])(?::([0-5][0-9]))?\s*(am|pm)\b', text)
    if match_ampm:
        hour = int(match_ampm.group(1))
        minute = match_ampm.group(2) or "00"
        ampm = match_ampm.group(3)
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        return f"{hour:02d}:{minute}"
    return None


def get_target_pending_appointment_note(quote=None):
    """Finds target pending appointment note matching quoted text or latest."""
    pending = get_pending_appointment_notes()
    if not pending:
        return None

    if quote and isinstance(quote, dict):
        quoted_text = quote.get("text", "") or ""
        if quoted_text:
            for mtime, path, content in pending:
                title, _, _ = parse_note_details(content)
                if title and title.lower() in quoted_text.lower():
                    return (mtime, path, content)
                if os.path.basename(path) in quoted_text:
                    return (mtime, path, content)

    return pending[0]


def handle_text_command(sender, text_msg, quote=None):
    """Processes interactive approval/rejection/rescheduling text commands."""
    cmd = text_msg.strip().lower()
    latest = get_target_pending_appointment_note(quote)

    if not latest:
        send_signal_message(sender, "ℹ️ No pending appointments found to process.")
        return

    mtime, note_path, content = latest
    fm, body = voice_harvester.extract_frontmatter_and_body(content)
    title, date_val, time_val = parse_note_details(content)

    if cmd in ("approve", "yes", "y", "да"):
        fm["status"] = "approved"
        new_yaml = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)
        new_content = f"---\n{new_yaml}---\n{body.lstrip()}"
        with FileLock(f"{note_path}.lock", timeout=10):
            voice_harvester.atomic_write(note_path, new_content)
        seafile_client.update_note("Appointments", os.path.basename(note_path), new_content)
        voice_harvester.check_and_sync_approved_notes()
        send_signal_message(sender, f"✅ Confirmed & Synced: '{title}' for {date_val} @ {time_val}")
        return

    if cmd in ("reject", "no", "n", "нет", "cancel", "отмена"):
        fm["status"] = "rejected"
        new_yaml = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)
        new_content = f"---\n{new_yaml}---\n{body.lstrip()}"
        with FileLock(f"{note_path}.lock", timeout=10):
            voice_harvester.atomic_write(note_path, new_content)
        seafile_client.update_note("Appointments", os.path.basename(note_path), new_content)
        send_signal_message(sender, f"❌ Cancelled appointment: '{title}'")
        return

    # Check for direct time change (e.g. '15:30' or '3pm')
    new_time = parse_spoken_time(cmd)
    if new_time:
        fm["status"] = "approved"
        fm["startTime"] = new_time
        fm["allDay"] = False
        new_yaml = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)
        new_content = f"---\n{new_yaml}---\n{body.lstrip()}"
        with FileLock(f"{note_path}.lock", timeout=10):
            voice_harvester.atomic_write(note_path, new_content)
        seafile_client.update_note("Appointments", os.path.basename(note_path), new_content)
        voice_harvester.check_and_sync_approved_notes()
        send_signal_message(sender, f"🕒 Rescheduled & Synced: '{title}' to {date_val} @ {new_time}")
        return

    send_signal_message(
        sender,
        f"❓ Command not understood for pending appointment '{title}'.\nReply 'approve', 'reject', or a new time (e.g. '16:00')."
    )


def process_incoming_voice_note(att_id, sender):
    """Worker task: downloads audio attachment, triggers harvester, and sends confirmation."""
    logger.info(f"Downloading attachment ID {att_id}...")
    filepath = download_attachment(att_id)
    if not filepath:
        if sender:
            send_signal_message(sender, "⚠️ Failed to download voice note attachment.")
        return

    success = voice_harvester.process_file(filepath)
    if success is True:
        latest_appt = get_target_pending_appointment_note()
        latest_agent = get_target_pending_agent_note()

        # Prioritize appointment prompt if a recent appointment was staged
        if latest_appt and (time.time() - latest_appt[0] < 60):
            _, _, content = latest_appt
            title, date_val, time_val = parse_note_details(content)
            conflict_status = check_calendar_conflicts(date_val, time_val)
            reply = (
                f"✅ Voice Note Processed & Staged in Obsidian!\n\n"
                f"📌 Staged Appointment: {title}\n"
                f"📅 Scheduled For: {date_val} @ {time_val}\n"
                f"{conflict_status}\n\n"
                f"Reply with your decision:\n"
                f"• 'approve' (or 'yes') -> Confirm & sync at {time_val}\n"
                f"• 'reject' (or 'no') -> Cancel appointment\n"
                f"• Or type any time (e.g. '16:00') -> Change start time & sync"
            )
        elif latest_agent and (time.time() - latest_agent[0] < 60):
            target, action = parse_agent_note_details(latest_agent[2])
            reply = (
                f"🤖 Agent Task Staged in AgentBacklog!\n\n"
                f"📌 Target: {target}\n"
                f"⚡ Action: {action}\n\n"
                f"⏳ Status: pending (queued for AI agent pickup)"
            )
        else:
            reply = "✅ Voice note processed & staged in Obsidian Inbox!"
    elif success == "dead_letter":
        reply = "⚠️ Voice note transcribed, but LLM structuring was unavailable. Raw transcript saved to Obsidian Inbox/Life/!"
    else:
        reply = "⚠️ Failed to process incoming voice note."

    if sender:
        send_signal_message(sender, reply)


def is_my_number(num_str):
    """Checks if phone number matches SIGNAL_PHONE_NUMBER."""
    if not num_str or not SIGNAL_PHONE_NUMBER:
        return False
    clean_target = num_str.replace(" ", "").replace("-", "").replace("+", "")
    clean_mine = SIGNAL_PHONE_NUMBER.replace(" ", "").replace("-", "").replace("+", "")
    return clean_target == clean_mine


def process_signal_envelope(envelope, loop):
    """Parses incoming WebSocket envelope and dispatches tasks asynchronously."""
    data = envelope.get("dataMessage", {})
    sync_data = envelope.get("syncMessage", {}).get("sentMessage", {})

    # Ignore group messages in voice ingest daemon
    group_info = data.get("groupInfo") or sync_data.get("groupInfo")
    if group_info:
        return

    source = envelope.get("source") or envelope.get("sourceNumber")
    sync_dest = sync_data.get("destination") or sync_data.get("destinationNumber")
    data_dest = data.get("destination") or data.get("destinationNumber")

    # Filter messages to only process Direct Messages / Note to Self
    if sync_dest and not is_my_number(sync_dest):
        return
    if data_dest and not is_my_number(data_dest):
        return
    if source and not is_my_number(source):
        return

    sender = source or SIGNAL_PHONE_NUMBER
    attachments = data.get("attachments", []) + sync_data.get("attachments", [])
    text_msg = data.get("message") or sync_data.get("message")
    quote = data.get("quote") or sync_data.get("quote")

    # Handle Audio Attachments
    if attachments:
        for att in attachments:
            content_type = (att.get("contentType") or att.get("mimeType") or "").lower()
            filename = (att.get("filename") or "").lower()
            att_id = att.get("id")

            if not att_id:
                continue

            is_audio = (
                any(t in content_type for t in ("audio", "ogg", "aac", "m4a", "wav", "mp4", "mpeg", "opus"))
                or filename.endswith((".ogg", ".m4a", ".wav", ".aac", ".mp3", ".mp4", ".opus"))
                or att.get("voiceNote") is True
            )

            if is_audio:
                logging.info(f"Received audio attachment (Type: '{content_type}', ID: '{att_id}')")
                if sender:
                    loop.run_in_executor(worker_pool, send_signal_message, sender, "⏳ Voice note received! Transcribing & processing...")
                # Dispatch background processing to thread pool
                loop.run_in_executor(worker_pool, process_audio_attachment_sync, att_id, sender)
                return

    # Handle Text Interactive Commands
    if text_msg and not attachments:
        logging.info(f"Received Signal text command: '{text_msg}' (quote: {quote})")
        loop.run_in_executor(worker_pool, handle_text_command, sender, text_msg, quote)


async def listen_signal_websocket():
    """Asynchronous WebSocket listener for Signal messages."""
    parsed = urlparse(SIGNAL_API_URL)
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    ws_host = parsed.netloc or "127.0.0.1:8080"
    ws_url = f"{ws_scheme}://{ws_host}/v1/receive/{SIGNAL_PHONE_NUMBER}"
    logging.info(f"Connecting to Signal WebSocket at {ws_url}...")
    loop = asyncio.get_running_loop()

    while True:
        try:
            async with websockets.connect(ws_url) as ws:
                logging.info("Connected to Signal WebSocket stream.")
                async for msg in ws:
                    try:
                        data = json.loads(msg)
                        env = data.get("envelope", {})
                        if env:
                            process_signal_envelope(env, loop)
                    except Exception as e:
                        logging.error(f"Error parsing WebSocket payload: {e}")
        except Exception as e:
            logging.warning(f"WebSocket connection error: {e}. Retrying in 5 seconds...")
            await asyncio.sleep(5)


if __name__ == "__main__":
    ensure_staging_dir()
    asyncio.run(listen_signal_websocket())
