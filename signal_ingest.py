#!/usr/bin/env python3
"""
Signal Voice Note Ingestion & Interactive Command Module for Voice Notes Pipeline
Connects to local signal-cli-rest-api via WebSocket:
1. Multi-User Authorization: Fuad (+994502214707) and Veronika (+994519325767).
2. Tri-Modal Shopping Ingress:
   - Voice Notes: Transcribed via Whisper, shopping intent extracted and added to rolling 12h list.
   - Text Messages: Natural grocery lists ('купи сыр, масло') or commands ('что купить', 'вычеркни хлеб').
   - Image Screenshots: Vision OCR (Gemini 2.5 Flash) extracts grocery items from screenshots/photos.
3. Sovereign Storage & Cloud Sync:
   - Obsidian: 'Life/Shopping List.md' + Seafile REST sync.
   - Google Tasks: 'Shopping' tasklist for mobile & smartwatch.
   - Appointments/Tasks: Obsidian Inbox + Google Calendar/Tasks.
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
import base64
import requests
import websockets
import yaml
from filelock import FileLock

import voice_harvester
import seafile_sync
import shopping_manager

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
VERONIKA_SIGNAL_NUMBER = os.environ.get("VERONIKA_SIGNAL_NUMBER", "+994519325767")
AUTHORIZED_SENDERS = [SIGNAL_PHONE_NUMBER, VERONIKA_SIGNAL_NUMBER]

STAGING_DIR = os.environ.get("VOICE_STAGING_DIR", "/tmp/signal_voice_staging/")
INBOX_DIR = os.environ.get("VOICE_INBOX_DIR", "/home/fuad/Seafile/Obsidian Vaults/VoiceNotes/Inbox/")

# Thread pool for offloading synchronous CPU / I/O tasks
worker_pool = ThreadPoolExecutor(max_workers=4)


FAMILY_NOTES_GROUP_ID = os.environ.get(
    "FAMILY_NOTES_GROUP_ID",
    "group.M0VLQVlkc3RqcHRTZnBPRGdNS0d0Yzd5bDVmR3A1WE9BZ3c1a1pvUU1Lcz0="
)
FAMILY_NOTES_INTERNAL_ID = "3EKAYdstjptSfpODgMKGtc7yl5fGp5XOAgw5kZoQMKs="


def is_family_notes_group(gid):
    """Checks if a group ID matches the authorized Family Notes group."""
    if not gid:
        return False
    clean = str(gid).strip()
    return clean in (FAMILY_NOTES_GROUP_ID, FAMILY_NOTES_INTERNAL_ID) or clean.removeprefix("group.") == FAMILY_NOTES_INTERNAL_ID


def ensure_staging_dir():
    os.makedirs(STAGING_DIR, exist_ok=True)


def normalize_phone(num_str):
    """Strips whitespace, hyphens, pluses, and parentheses for uniform comparison."""
    if not num_str:
        return ""
    return re.sub(r'[\s\-\+\(\)]', '', str(num_str))


def is_my_number(num_str):
    """Checks if phone number matches primary SIGNAL_PHONE_NUMBER (Fuad)."""
    return normalize_phone(num_str) == normalize_phone(SIGNAL_PHONE_NUMBER)


def is_authorized_number(num_str):
    """Checks if phone number is in AUTHORIZED_SENDERS whitelist."""
    clean = normalize_phone(num_str)
    if not clean:
        return False
    for auth in AUTHORIZED_SENDERS:
        if clean == normalize_phone(auth):
            return True
    return False


def get_sender_name(num_str):
    """Returns friendly sender name ('Veronika' or 'Fuad')."""
    clean = normalize_phone(num_str)
    if clean == normalize_phone(VERONIKA_SIGNAL_NUMBER):
        return "Veronika"
    return "Fuad"


def send_signal_message(recipient, message, group_id=None):
    """Sends a Signal text message via signal-cli REST API to a user or group."""
    if not SIGNAL_PHONE_NUMBER:
        logging.warning("SIGNAL_PHONE_NUMBER not set. Skipping response dispatch.")
        return False

    url = f"{SIGNAL_API_URL.rstrip('/')}/v2/send"
    target = group_id if group_id else (recipient or SIGNAL_PHONE_NUMBER)
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
        logging.error(f"Failed to send Signal message to {target}: {e}")
        return False


def download_attachment(attachment_id):
    """Downloads attachment from signal-cli-rest-api."""
    ensure_staging_dir()
    url = f"{SIGNAL_API_URL.rstrip('/')}/v1/attachments/{attachment_id}"
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        content_type = r.headers.get("content-type", "").lower()
        if "image" in content_type:
            ext = ".jpg"
        elif "ogg" in content_type or "opus" in content_type:
            ext = ".ogg"
        elif "wav" in content_type:
            ext = ".wav"
        else:
            ext = ".bin"

        filename = f"signal_att_{int(time.time())}_{attachment_id[:8]}{ext}"
        filepath = os.path.join(STAGING_DIR, filename)
        with open(filepath, "wb") as f:
            f.write(r.content)
        logging.info(f"Downloaded Signal attachment {attachment_id} to {filepath}")
        return filepath
    except Exception as e:
        logging.error(f"Failed to download attachment {attachment_id}: {e}")
        return None


def check_calendar_conflicts(date_str, start_time_str):
    """Performs conflict check against Google Calendar if available."""
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
    """Scans INBOX_DIR for notes with pending status in appointments category."""
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
        if line.strip().startswith("# Target"):
            if i + 1 < len(lines):
                target = lines[i + 1].strip()
        elif line.strip().startswith("# Requested Agent Action"):
            if i + 1 < len(lines):
                action = lines[i + 1].strip()
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


# =====================================================================
# GROCERY & SHOPPING EXTRACTION ENGINES (Text, Vision & Voice)
# =====================================================================

def extract_grocery_items_from_image(filepath):
    """Sends image to multimodal Vision model (Gemini 2.5 Flash via OpenRouter) to extract grocery items."""
    try:
        with open(filepath, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode("utf-8")

        api_key = voice_harvester.get_openrouter_api_key()
        if not api_key:
            logging.error("OpenRouter API key missing for image vision extraction.")
            return []

        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {
            "model": "google/gemini-2.5-flash",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Extract all grocery and shopping items from this screenshot/note image. "
                                "Return ONLY a valid JSON array of strings in the original language (Russian/Azerbaijani/English), "
                                "e.g. [\"Молоко\", \"Хлеб\", \"Сыр\"]. Do NOT output any markdown, explanations, or code blocks."
                            )
                        },
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}}
                    ]
                }
            ],
            "temperature": 0.1
        }
        resp = requests.post(url, headers=headers, json=payload, timeout=25)
        resp.raise_for_status()
        content = resp.json()['choices'][0]['message']['content']
        clean_json = content.strip().strip("`").lstrip("json\n").strip()
        data = json.loads(clean_json)
        if isinstance(data, list):
            return [str(i).strip() for i in data if str(i).strip()]
        return []
    except Exception as e:
        logging.error(f"Image vision extraction failed: {e}")
        return []


def extract_grocery_items_from_text(text):
    """Uses LLM to extract grocery/shopping items from spoken transcript or text."""
    messages = [
        {
            "role": "system",
            "content": (
                "You are an expert grocery list parser. Extract all items that need to be bought from the input text. "
                "Return ONLY a valid JSON array of strings in original language (e.g. [\"Молоко\", \"Хлеб\"]). "
                "Omit conversational fillers, greetings, and quantity words unless part of the item."
            )
        },
        {
            "role": "user",
            "content": text
        }
    ]

    raw_res = voice_harvester.extract_llm_cloud_openrouter(messages)
    if not raw_res:
        raw_res = voice_harvester.extract_llm_vertex_gemini(messages)

    if raw_res:
        try:
            clean_json = raw_res.strip().strip("`").lstrip("json\n").strip()
            data = json.loads(clean_json)
            if isinstance(data, list) and data:
                return [str(i).strip() for i in data if str(i).strip()]
        except Exception:
            pass

    # Deterministic regex fallback
    cleaned = re.sub(
        r'^(?:купи|купить|в список|список покупок|покупки|добавь в покупки|надо купить|напоминание|buy|add to shopping list)[:\s]*',
        '', text, flags=re.IGNORECASE
    ).strip()
    raw_items = re.split(r'[\n,;]+', cleaned)
    return [i.strip().strip("•-* \t") for i in raw_items if i.strip()]


def process_incoming_image(att_id, sender, group_id=None):
    """Downloads image, extracts grocery items via Vision OCR, and appends to shopping list."""
    sender_name = get_sender_name(sender)
    logging.info(f"Processing image attachment from {sender_name}...")
    filepath = download_attachment(att_id)
    if not filepath:
        send_signal_message(sender, "⚠️ Не удалось загрузить изображение.", group_id=group_id)
        return

    items = extract_grocery_items_from_image(filepath)
    if items:
        res = shopping_manager.add_items(items, source=f"{sender_name} (Screenshot)")
        added_lines = "\n".join([f"• {itm}" for itm in res.get("added", [])])
        reply = (
            f"📸 **Список покупок распознан и добавлен ({sender_name}):**\n"
            f"{added_lines}\n\n"
            f"🛒 Всего в активном списке: {res.get('total_active', 0)}"
        )
        send_signal_message(sender, reply, group_id=group_id)
        # Cross-notify Fuad if Veronika submitted via personal direct message
        if not group_id and sender_name == "Veronika" and not is_my_number(sender):
            send_signal_message(SIGNAL_PHONE_NUMBER, f"🛒 **Вероника добавила в список покупок (по фото):**\n{added_lines}")
    else:
        send_signal_message(sender, "⚠️ Не удалось распознать список покупок на картинке.", group_id=group_id)


def handle_text_command(sender, text_msg, quote=None, group_id=None):
    """Processes interactive approval/rejection or grocery text commands."""
    sender_name = get_sender_name(sender)
    cmd = text_msg.strip().lower()

    # 1. Shopping List Query ('что купить?', 'список покупок', 'покупки')
    if any(q in cmd for q in ("что купить", "список покупок", "список", "покупки", "what to buy", "shopping list")):
        active = shopping_manager.get_active_items()
        items = active.get("items", [])
        if not items:
            send_signal_message(sender, "🛒 Список покупок пуст! Все куплено или сессия завершена.", group_id=group_id)
        else:
            lines = "\n".join([f"- [ ] {itm}" for itm in items])
            msg = f"🛒 **Текущий список покупок ({len(items)}):**\n{lines}\n\n(Чтобы вычеркнуть, напиши 'купил <товар>')"
            send_signal_message(sender, msg, group_id=group_id)
        return

    # 2. Check off purchased item ('купил молоко', 'вычеркни хлеб', 'купила сыр')
    checkoff_match = re.match(r'^(?:купил|купила|вычеркни|купили|bought|check off)\s+(.+)$', cmd, re.IGNORECASE)
    if checkoff_match:
        target_item = checkoff_match.group(1).strip()
        comp = shopping_manager.complete_item(target_item)
        if comp.get("status") == "success":
            reply = f"✅ Вычеркнуто из покупок: **{comp.get('completed')}**\n🛒 Осталось купить: {comp.get('remaining_active', 0)}"
            send_signal_message(sender, reply, group_id=group_id)
            if not group_id and sender_name == "Veronika" and not is_my_number(sender):
                send_signal_message(SIGNAL_PHONE_NUMBER, f"🛒 **Вероника вычеркнула:** {comp.get('completed')}")
        else:
            send_signal_message(sender, f"ℹ️ Товар '{target_item}' не найден в активном списке покупок.", group_id=group_id)
        return

    # 3. Add to Shopping List ('купи сыр, хлеб', 'добавь в покупки: ...', or multi-line grocery list)
    grocery_trigger = re.match(r'^(?:купи|купить|в покупки|добавь в покупки|в список)[:\s]+(.+)$', text_msg.strip(), re.IGNORECASE)
    # If Veronika sends a message with commas, newlines, or food items, treat as grocery addition
    is_veronika_grocery = (
        sender_name == "Veronika"
        and cmd not in ("approve", "yes", "reject", "no", "да", "нет")
        and not parse_spoken_time(cmd)
    )

    if grocery_trigger or is_veronika_grocery:
        raw_to_parse = grocery_trigger.group(1) if grocery_trigger else text_msg
        items = extract_grocery_items_from_text(raw_to_parse)
        if items:
            res = shopping_manager.add_items(items, source=f"{sender_name} (Text)")
            added_lines = "\n".join([f"• {itm}" for itm in res.get("added", [])])
            reply = (
                f"🛒 **Добавлено в список покупок ({sender_name}):**\n"
                f"{added_lines}\n\n"
                f"Всего в активном списке: {res.get('total_active', 0)}"
            )
            send_signal_message(sender, reply, group_id=group_id)
            if not group_id and sender_name == "Veronika" and not is_my_number(sender):
                send_signal_message(SIGNAL_PHONE_NUMBER, f"🛒 **Вероника добавила в список покупок:**\n{added_lines}")
            return

    # 4. Interactive Appointment Commands (approve, reject, reschedule)
    latest = get_target_pending_appointment_note(quote)
    if latest:
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
            send_signal_message(sender, f"✅ Confirmed & Synced: '{title}' for {date_val} @ {time_val}", group_id=group_id)
            return

        if cmd in ("reject", "no", "n", "нет", "cancel", "отмена"):
            fm["status"] = "rejected"
            new_yaml = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)
            new_content = f"---\n{new_yaml}---\n{body.lstrip()}"
            with FileLock(f"{note_path}.lock", timeout=10):
                voice_harvester.atomic_write(note_path, new_content)
            seafile_client.update_note("Appointments", os.path.basename(note_path), new_content)
            send_signal_message(sender, f"❌ Cancelled appointment: '{title}'", group_id=group_id)
            return

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
            send_signal_message(sender, f"🕒 Rescheduled & Synced: '{title}' to {date_val} @ {new_time}", group_id=group_id)
            return

    # Suppress unrecognized error spam:
    # In groups or normal conversations, stay completely silent if no command matched.
    # Only show fallback in private Note-to-Self if text explicitly looked like an attempted command.
    if not group_id and is_my_number(sender) and text_msg.strip().startswith(("!", "/", "bot", "dixie")):
        send_signal_message(
            sender,
            f"❓ Команда не распознана.\n"
            f"• Список покупок: 'что купить?' или 'купи <товары>'\n"
            f"• Вычеркнуть: 'купил <товар>'\n"
            f"• Подтверждение встреч: 'approve', 'reject' или время (например '16:00')"
        )


def process_incoming_voice_note(att_id, sender, group_id=None):
    """Worker task: downloads audio attachment, triggers harvester or shopping manager, sends reply."""
    sender_name = get_sender_name(sender)
    logger.info(f"Downloading audio attachment ID {att_id} from {sender_name}...")
    filepath = download_attachment(att_id)
    if not filepath:
        if sender:
            send_signal_message(sender, "⚠️ Не удалось загрузить аудиосообщение.", group_id=group_id)
        return

    # 1. Transcribe audio
    raw_transcript, lang, prob = voice_harvester.transcribe_audio(filepath)
    if not raw_transcript.strip():
        send_signal_message(sender, "⚠️ В аудиосообщении не удалось распознать речь.", group_id=group_id)
        return

    # 2. Check if voice note is grocery/shopping related
    shopping_pattern = re.search(
        r'\b(список покупок|покупки|купи|купить|в магазин|продукты|купите|добавь в покупки|надо купить|shopping)\b',
        raw_transcript,
        re.IGNORECASE
    )
    if shopping_pattern:
        items = extract_grocery_items_from_text(raw_transcript)
        if items:
            res = shopping_manager.add_items(items, source=f"{sender_name} (Voice)")
            added_lines = "\n".join([f"• {itm}" for itm in res.get("added", [])])
            reply = (
                f"🛒 **Добавлено в список покупок ({sender_name}):**\n"
                f"{added_lines}\n\n"
                f"Всего в активном списке: {res.get('total_active', 0)}"
            )
            send_signal_message(sender, reply, group_id=group_id)
            if not group_id and sender_name == "Veronika" and not is_my_number(sender):
                send_signal_message(SIGNAL_PHONE_NUMBER, f"🛒 **Вероника добавила в список покупок (голосом):**\n{added_lines}")
            voice_harvester.archive_file(filepath)
            return

    # 3. Standard Voice Note Pipeline (Appointments, Tasks, Technical, Life)
    success = voice_harvester.process_file(filepath, author=sender_name)
    if success is True:
        latest_appt = get_target_pending_appointment_note()
        latest_agent = get_target_pending_agent_note()

        if latest_appt and (time.time() - latest_appt[0] < 60):
            _, _, content = latest_appt
            title, date_val, time_val = parse_note_details(content)
            conflict_status = check_calendar_conflicts(date_val, time_val)
            reply = (
                f"✅ Заметка обработана и сохранена в Obsidian!\n\n"
                f"📌 Запланирована встреча: {title}\n"
                f"📅 Дата: {date_val} @ {time_val}\n"
                f"{conflict_status}\n\n"
                f"Решение:\n"
                f"• 'approve' / 'да' -> Подтвердить и синхронизировать\n"
                f"• 'reject' / 'нет' -> Отменить встречу\n"
                f"• Либо напиши новое время (например '16:00')"
            )
        elif latest_agent and (time.time() - latest_agent[0] < 60):
            target, action = parse_agent_note_details(latest_agent[2])
            reply = (
                f"🤖 Задача для агента добавлена в AgentBacklog!\n\n"
                f"📌 Контекст: {target}\n"
                f"⚡ Действие: {action}\n\n"
                f"⏳ Статус: pending (в очереди)"
            )
        else:
            reply = f"✅ Голосовая заметка сохранена в Obsidian Inbox (Автор: {sender_name})!"
    elif success == "dead_letter":
        reply = "⚠️ Голосовая заметка расшифрована, но LLM недоступен. Черновой текст сохранен в Inbox/Life/."
    else:
        reply = "⚠️ Не удалось обработать голосовую заметку."

    if sender:
        send_signal_message(sender, reply, group_id=group_id)


def process_signal_envelope(envelope, loop):
    """Parses incoming WebSocket envelope and dispatches tasks asynchronously."""
    data = envelope.get("dataMessage", {})
    sync_data = envelope.get("syncMessage", {}).get("sentMessage", {})

    # Check for group message
    group_info = data.get("groupInfo") or sync_data.get("groupInfo")
    raw_group_id = group_info.get("groupId") if group_info else None

    target_group = None
    if raw_group_id:
        if is_family_notes_group(raw_group_id):
            target_group = FAMILY_NOTES_GROUP_ID
        else:
            logging.debug(f"Ignoring non-family group: {raw_group_id}")
            return

    source = envelope.get("source") or envelope.get("sourceNumber")
    sync_dest = sync_data.get("destination") or sync_data.get("destinationNumber")
    data_dest = data.get("destination") or data.get("destinationNumber")

    # SENDER VALIDATION & OPTION B ISOLATION
    if target_group:
        # Shared Group Channel: Sender must be authorized (Fuad or Veronika)
        sender = source or sync_dest or data_dest or SIGNAL_PHONE_NUMBER
        if not is_authorized_number(sender):
            logging.debug(f"Ignoring group message from unauthorized sender: {sender}")
            return
        reply_group = target_group
    else:
        # 1-on-1 Direct Message Channel:
        # 1. Drop any outgoing sync message sent to another person (Veronika, friends, clients)
        if sync_dest and not is_my_number(sync_dest):
            logging.debug(f"Dropping 1-on-1 outgoing sync message to {sync_dest} (privacy guard)")
            return

        # 2. Drop any incoming 1-on-1 DM from anyone else (Veronika, friends, strangers)
        sender = source or sync_dest or data_dest or SIGNAL_PHONE_NUMBER
        if not is_my_number(sender):
            logging.debug(f"Dropping 1-on-1 incoming DM from {sender} (Option B isolation)")
            return

        reply_group = None

    sender_name = get_sender_name(sender)
    attachments = data.get("attachments", []) + sync_data.get("attachments", [])
    text_msg = data.get("message") or sync_data.get("message")
    quote = data.get("quote") or sync_data.get("quote")

    # 1. Handle Attachments
    if attachments:
        for att in attachments:
            content_type = (att.get("contentType") or att.get("mimeType") or "").lower()
            filename = (att.get("filename") or "").lower()
            att_id = att.get("id")

            if not att_id:
                continue

            # Check if Audio
            is_audio = (
                any(t in content_type for t in ("audio", "ogg", "aac", "m4a", "wav", "mp4", "mpeg", "opus"))
                or filename.endswith((".ogg", ".m4a", ".wav", ".aac", ".mp3", ".mp4", ".opus"))
                or att.get("voiceNote") is True
            )
            if is_audio:
                logging.info(f"Received audio attachment from {sender_name} in {'group' if reply_group else 'Note-to-Self'} (ID: '{att_id}')")
                if sender:
                    loop.run_in_executor(worker_pool, send_signal_message, sender, "⏳ Голосовое сообщение получено! Обрабатываю...", reply_group)
                loop.run_in_executor(worker_pool, process_incoming_voice_note, att_id, sender, reply_group)
                return

            # Check if Image (screenshot / photo of grocery list)
            is_image = (
                any(t in content_type for t in ("image", "jpeg", "jpg", "png", "webp", "heic"))
                or filename.endswith((".jpg", ".jpeg", ".png", ".webp", ".heic"))
            )
            if is_image:
                logging.info(f"Received image attachment from {sender_name} in {'group' if reply_group else 'Note-to-Self'} (ID: '{att_id}')")
                if sender:
                    loop.run_in_executor(worker_pool, send_signal_message, sender, "📸 Изображение получено! Распознаю список покупок...", reply_group)
                loop.run_in_executor(worker_pool, process_incoming_image, att_id, sender, reply_group)
                return

    # 2. Handle Text Commands
    if text_msg and not attachments:
        logging.info(f"Received Signal text command from {sender_name} in {'group' if reply_group else 'Note-to-Self'}: '{text_msg}' (quote: {quote})")
        loop.run_in_executor(worker_pool, handle_text_command, sender, text_msg, quote, reply_group)


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
