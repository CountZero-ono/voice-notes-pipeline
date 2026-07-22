#!/usr/bin/env python3
"""
Signal Voice Note Ingestion & Interactive Command Module for Voice Notes Pipeline
Connects to local signal-cli-rest-api via WebSocket:
1. Sends 2-stage instant receipt & completion alerts.
2. Performs live Radicale CalDAV conflict checks.
3. Handles text replies ('approve', 'reject', '15:30') to sync directly to Radicale CalDAV.
"""

import os
import sys
import time
import json
import re
import logging
import asyncio
import requests
import websockets
import voice_harvester

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [SIGNAL-INGEST] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.expanduser("~/OCProjects/voice-notes-pipeline/harvester.log"))
    ]
)

SIGNAL_API_URL = os.environ.get("SIGNAL_API_URL", "http://127.0.0.1:8080")
SIGNAL_PHONE_NUMBER = os.environ.get("SIGNAL_PHONE_NUMBER", "+994502214707")
STAGING_DIR = os.environ.get("VOICE_STAGING_DIR", "/tmp/signal_voice_staging/")
INBOX_DIR = os.environ.get("VOICE_INBOX_DIR", "/home/fuad/Seafile/Obsidian Vaults/VoiceNotes/Inbox/")

def ensure_staging_dir():
    os.makedirs(STAGING_DIR, exist_ok=True)

def send_signal_message(recipient, message):
    if not SIGNAL_PHONE_NUMBER:
        logging.warning("SIGNAL_PHONE_NUMBER not set. Skipping response dispatch.")
        return False
    url = f"{SIGNAL_API_URL.rstrip('/')}/v2/send"
    payload = {
        "number": SIGNAL_PHONE_NUMBER,
        "recipients": [recipient],
        "message": message
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        logging.info(f"Sent Signal response to {recipient}")
        return True
    except Exception as e:
        logging.error(f"Failed to send Signal message: {e}")
        return False

def download_attachment(attachment_id):
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

def check_radicale_conflicts(date_str, start_time_str):
    if not date_str or date_str == "Today":
        return "🟢 **Calendar Free**"
    url = voice_harvester.RADICALE_CALENDAR_URL
    auth = voice_harvester.RADICALE_AUTH
    date_clean = date_str.replace("-", "")
    conflicts = []
    
    try:
        r = requests.request("PROPFIND", url, headers={"Depth": "1"}, auth=auth, timeout=5)
        if r.status_code == 207:
            hrefs = re.findall(r'<a:href>([^<]+\.ics)</a:href>', r.text) or re.findall(r'<D:href>([^<]+\.ics)</D:href>', r.text)
            for href in hrefs:
                event_url = f"http://192.168.1.30:5232{href}" if href.startswith("/") else href
                try:
                    ev_r = requests.get(event_url, auth=auth, timeout=3)
                    if ev_r.status_code == 200:
                        ics_text = ev_r.text
                        if date_clean in ics_text or date_str in ics_text:
                            sum_m = re.search(r'SUMMARY:(.+)', ics_text)
                            summary = sum_m.group(1).strip() if sum_m else "Existing Event"
                            conflicts.append(summary)
                except Exception:
                    pass
    except Exception as e:
        logging.warning(f"Could not check Radicale conflicts: {e}")
        
    if conflicts:
        return f"⚠️ **Calendar Conflict:** Existing entry on {date_str}: '{conflicts[0]}'"
    return "🟢 **Calendar Free** (No conflicts found)"

def get_pending_appointment_notes():
    if not os.path.exists(INBOX_DIR):
        return []
    pending_notes = []
    for root, dirs, files in os.walk(INBOX_DIR):
        for file in files:
            if file.endswith(".md"):
                full_path = os.path.join(root, file)
                try:
                    with open(full_path, "r", encoding="utf-8") as f:
                        content = f.read()
                    if 'status: "pending"' in content or 'status: pending' in content:
                        mtime = os.path.getmtime(full_path)
                        pending_notes.append((mtime, full_path, content))
                except Exception:
                    pass
    pending_notes.sort(key=lambda x: x[0], reverse=True)
    return pending_notes

def get_target_pending_appointment_note(quote=None):
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
                    
    # Fallback to latest
    return pending[0]

def parse_note_details(content):
    title = "Appointment"
    date_val = "Today"
    time_val = "TBD"
    
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            fm = parts[1]
            for line in fm.splitlines():
                line = line.strip()
                if ":" in line and not line.startswith("-"):
                    k, v = line.split(":", 1)
                    k = k.strip().lower()
                    v = v.strip().replace('"', '').replace("'", "")
                    if k == "title":
                        title = v
                    elif k == "date":
                        date_val = v
                    elif k == "starttime":
                        time_val = v
    return title, date_val, time_val

def handle_text_command(sender, text_msg, quote=None):
    cmd = text_msg.strip().lower()
    target = get_target_pending_appointment_note(quote=quote)
    
    if not target:
        if any(w in cmd for w in ("approve", "yes", "reject", "no", "reschedule")):
            send_signal_message(sender, "ℹ️ No pending appointments found in Obsidian Inbox.")
        return
        
    mtime, note_path, content = target
    title, date_val, time_val = parse_note_details(content)
    
    # 1. APPROVE COMMAND
    if cmd in ("approve", "yes", "y", "ok", "confirm", "synced"):
        new_content = content.replace('status: "pending"', 'status: "approved"').replace('status: pending', 'status: "approved"')
        with open(note_path, "w", encoding="utf-8") as f:
            f.write(new_content)
            
        voice_harvester.check_and_sync_approved_notes()
        send_signal_message(sender, f"✅ Approved! Synced '{title}' ({date_val} @ {time_val}) to calendar.")
        return
        
    # 2. REJECT COMMAND
    if cmd in ("reject", "no", "n", "cancel"):
        new_content = content.replace('status: "pending"', 'status: "rejected"').replace('status: pending', 'status: "rejected"')
        with open(note_path, "w", encoding="utf-8") as f:
            f.write(new_content)
        send_signal_message(sender, f"❌ Rejected appointment '{title}'. Marked as rejected in Obsidian.")
        return
        
    # 3. RESCHEDULE COMMAND (e.g. "15:30" or "reschedule 15:30")
    time_match = re.search(r'\b([0-1]?[0-9]|2[0-3]):[0-5][0-9]\b', cmd)
    if time_match or "reschedule" in cmd:
        new_time = time_match.group(0) if time_match else "15:00"
        
        lines = content.splitlines()
        new_lines = []
        in_fm = False
        for line in lines:
            if line.strip() == "---":
                in_fm = not in_fm
                new_lines.append(line)
                continue
            if in_fm and line.strip().lower().startswith("starttime:"):
                new_lines.append(f'startTime: "{new_time}"')
            elif in_fm and line.strip().lower().startswith("status:"):
                new_lines.append('status: "approved"')
            else:
                new_lines.append(line)
                
        new_content = "\n".join(new_lines)
        with open(note_path, "w", encoding="utf-8") as f:
            f.write(new_content)
            
        voice_harvester.check_and_sync_approved_notes()
        send_signal_message(sender, f"📅 Rescheduled '{title}' to {new_time} ({date_val}) & synced to calendar!")
        return

def process_signal_envelope(envelope):
    data = envelope.get("dataMessage", {})
    sync_data = envelope.get("syncMessage", {}).get("sentMessage", {})
    
    source = envelope.get("source") or envelope.get("sourceNumber")
    sync_dest = sync_data.get("destination") or sync_data.get("destinationNumber")
    data_dest = data.get("destination") or data.get("destinationNumber")
    
    clean_my_num = SIGNAL_PHONE_NUMBER.replace(" ", "").replace("-", "")
    
    # 1. If sent by Fuad to a friend (syncMessage destination is a friend's number), ignore!
    if sync_dest:
        clean_dest = sync_dest.replace(" ", "").replace("-", "")
        if clean_dest != clean_my_num:
            logging.info(f"Ignoring voice/text sent to friend ({sync_dest})")
            return

    # 2. If dataMessage destination is a friend's number, ignore!
    if data_dest:
        clean_data_dest = data_dest.replace(" ", "").replace("-", "")
        if clean_data_dest != clean_my_num:
            logging.info(f"Ignoring message targeted at friend ({data_dest})")
            return

    # 3. If received from an external contact (source is a friend's number), ignore!
    if source:
        clean_source = source.replace(" ", "").replace("-", "")
        if clean_source != clean_my_num:
            logging.info(f"Ignoring message received from external contact: {source}")
            return

    sender = SIGNAL_PHONE_NUMBER
    attachments = data.get("attachments", []) + sync_data.get("attachments", [])
    text_msg = data.get("message") or sync_data.get("message")
    quote = data.get("quote") or sync_data.get("quote")
    
    # Process Audio Attachments
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
                logging.info(f"Processing audio attachment from self (Type: '{content_type}', ID: '{att_id}')")
                if sender:
                    send_signal_message(sender, "⏳ Voice note received! Transcribing & processing...")
                    
                filepath = download_attachment(att_id)
                if filepath:
                    success = voice_harvester.process_file(filepath)
                    if success:
                        latest = get_target_pending_appointment_note()
                        if latest:
                            _, _, content = latest
                            title, date_val, time_val = parse_note_details(content)
                            conflict_status = check_radicale_conflicts(date_val, time_val)
                            reply = f"✅ Voice Note Processed & Staged in Obsidian!\n\n📌 Staged Appointment: {title}\n📅 Scheduled For: {date_val} @ {time_val}\n{conflict_status}\n\nReply with your decision:\n• 'approve' (or 'yes') -> Confirm & sync at {time_val}\n• 'reject' (or 'no') -> Cancel appointment\n• Or type any time (e.g. '16:00') -> Change start time & sync"
                        else:
                            reply = "✅ Voice note processed & staged in Obsidian Inbox!"
                    else:
                        reply = "⚠️ Failed to process incoming voice note."
                    if sender:
                        send_signal_message(sender, reply)
                    return

    # Process Text Interactive Commands
    if text_msg and not attachments:
        logging.info(f"Received Signal text command from {sender}: '{text_msg}' (quote: {quote})")
        handle_text_command(sender, text_msg, quote=quote)

async def listen_signal_websocket():
    ws_url = f"ws://127.0.0.1:8080/v1/receive/{SIGNAL_PHONE_NUMBER}"
    logging.info(f"Connecting to Signal WebSocket at {ws_url}...")
    while True:
        try:
            async with websockets.connect(ws_url) as ws:
                logging.info("Connected to Signal WebSocket stream.")
                async for msg in ws:
                    try:
                        data = json.loads(msg)
                        env = data.get("envelope", {})
                        if env:
                            process_signal_envelope(env)
                    except Exception as e:
                        logging.error(f"Error parsing WebSocket payload: {e}")
        except Exception as e:
            logging.warning(f"WebSocket connection error: {e}. Retrying in 5 seconds...")
            await asyncio.sleep(5)

if __name__ == "__main__":
    ensure_staging_dir()
    asyncio.run(listen_signal_websocket())
