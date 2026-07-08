#!/usr/bin/env python3
"""
Voice Notes Harvester & Thought Conveyor
Processes local multi-lingual voice notes on CPU, classifies and cleans them using a local LLM,
and deposits formatted notes in categorized subfolders inside the Obsidian Vault Inbox.
"""

import os
import sys
import time
import json
import logging
from datetime import datetime
import requests

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

# LLM Config
LLM_API_URL = os.environ.get("LLM_API_URL", "http://127.0.0.1:1235/v1/chat/completions") # Port 1235 maps to Qwen 35B
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen")

# Whisper Config
WHISPER_MODEL_NAME = os.environ.get("WHISPER_MODEL", "large-v3-turbo")
WHISPER_THREADS = int(os.environ.get("WHISPER_THREADS", "4"))

# Supported Extensions
SUPPORTED_EXTENSIONS = (".mp3", ".wav", ".m4a")

# Global Whisper Model Instance
whisper_model = None

def load_whisper():
    global whisper_model
    if whisper_model is None:
        logging.info("Lazy-loading faster-whisper package...")
        try:
            from faster_whisper import WhisperModel
            logging.info(f"Initializing WhisperModel '{WHISPER_MODEL_NAME}' on CPU...")
            # Using CPU, compute_type="int8" for speed and efficiency
            whisper_model = WhisperModel(
                WHISPER_MODEL_NAME,
                device="cpu",
                compute_type="int8",
                cpu_threads=WHISPER_THREADS
            )
            logging.info("Whisper model loaded successfully on CPU.")
        except ImportError as e:
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
    """
    Ensures that Seafile has finished writing/syncing the file by
    monitoring its file size until it remains constant.
    """
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
            # File might temporarily lock or be inaccessible
            pass
        time.sleep(check_interval)
        
    logging.warning(f"File stabilization timed out for {filepath}.")
    return False

def transcribe_audio(filepath):
    """
    Transcribes the audio file using faster-whisper on CPU.
    Returns: (raw_transcript_text, detected_language, language_probability)
    """
    model = load_whisper()
    logging.info(f"Transcribing audio: {filepath}")
    
    # Run Whisper transcription
    segments, info = model.transcribe(filepath, beam_size=5)
    
    raw_text_parts = []
    for segment in segments:
        raw_text_parts.append(segment.text)
        
    raw_transcript = " ".join(raw_text_parts).strip()
    logging.info(f"ASR complete. Language detected: {info.language} ({info.language_probability:.2f})")
    
    return raw_transcript, info.language, info.language_probability

def clean_and_extract_llm(raw_text):
    """
    Queries the local LLM server to clean up the transcript and extract tasks/events.
    """
    # Load system prompt
    if not os.path.exists(SYSTEM_PROMPT_PATH):
        logging.error(f"System prompt file not found at {SYSTEM_PROMPT_PATH}")
        return None
        
    with open(SYSTEM_PROMPT_PATH, "r", encoding="utf-8") as f:
        system_prompt = f.read()

    today_str = datetime.now().strftime("%Y-%m-%d")
    
    # Format instruction payload
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Today's Reference Date: {today_str}\n\nRaw Transcription Text:\n{raw_text}"}
    ]
    
    headers = {"Content-Type": "application/json"}
    payload = {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": 0.1,  # Low temperature for highly structured tasks
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

def parse_category_from_llm(llm_content):
    """
    Extracts the category property from YAML frontmatter in LLM output.
    Supported categories: 'appointments', 'technical', 'life'.
    Defaults to 'life' if not found or invalid.
    """
    content = llm_content.strip()
    if not content.startswith("---"):
        return "life"
        
    parts = content.split("---", 2)
    if len(parts) < 3:
        return "life"
        
    frontmatter = parts[1]
    for line in frontmatter.splitlines():
        line = line.strip()
        if line.startswith("category:"):
            category = line.split(":", 1)[1].strip().lower()
            category = category.replace('"', '').replace("'", "")
            if category in ("appointments", "technical", "life"):
                return category
    return "life"

def write_to_inbox(original_filename, detected_lang, original_text, llm_content):
    """
    Writes the structured output to the categorized directory in the Obsidian Vault Inbox.
    """
    category = parse_category_from_llm(llm_content)
    category_folder = category.capitalize() # Appointments, Technical, Life
    target_dir = os.path.join(INBOX_DIR, category_folder)
    
    os.makedirs(target_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base_name, _ = os.path.splitext(original_filename)
    output_filename = f"VoiceNote-{timestamp}.md"
    output_path = os.path.join(target_dir, output_filename)
    
    note_content = ""
    has_yaml = llm_content.strip().startswith("---")
    
    if not has_yaml:
        # Prepend default frontmatter if LLM omitted it
        note_content += f"""---
category: {category}
title: "Voice Note: {base_name}"
date_created: "{datetime.now().strftime('%Y-%m-%d')}"
tags: ["#voicenote", "#inbox"]
---
"""
    note_content += llm_content
    
    # Append raw data and processing metadata at the bottom for record keeping
    note_content += f"\n\n---\n## Harvester Metadata\n"
    note_content += f"- **Original Audio file:** `{original_filename}`\n"
    note_content += f"- **Detected Language:** `{detected_lang}`\n"
    note_content += f"- **Processed At:** `{datetime.now().isoformat()}`\n\n"
    note_content += f"### Original Raw Transcription\n"
    note_content += f"> {original_text}\n"
    
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(note_content)
        
    logging.info(f"Saved note to {category_folder} Inbox: {output_path}")
    return output_path

def process_file(filepath):
    """
    Fully orchestrates transcription and LLM processing of a single audio file.
    """
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
            
            # File stats for checking state
            try:
                stat = os.stat(filepath)
                mtime = stat.st_mtime
                size = stat.st_size
            except OSError:
                continue
                
            # Skip if file was already processed with same stats
            if filename in state:
                if state[filename].get("size") == size and state[filename].get("mtime") == mtime:
                    continue
            
            logging.info(f"Detected new or modified file: {filename}")
            
            # Wait until Seafile finishes synchronization
            if not wait_for_file_to_stabilize(filepath):
                continue
                
            # Re-read stats after stabilization
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
                
        time.sleep(5)

if __name__ == "__main__":
    try:
        monitor_loop()
    except KeyboardInterrupt:
        logging.info("Harvester terminated by user.")
        sys.exit(0)
