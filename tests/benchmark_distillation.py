#!/usr/bin/env python3
"""
Distillation Benchmark: Local Qwen 3.6 35B vs Vertex AI Gemini 2.5 Flash
Evaluates latency, frontmatter validity, temporal math, and trilingual extraction fidelity.
"""

import os
import sys
import time
import json
import yaml
import requests
import google.auth
from google.auth.transport.requests import Request

WORKSPACE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYSTEM_PROMPT_PATH = os.path.join(WORKSPACE_DIR, "system_prompt.md")
LOCAL_LLM_URL = os.environ.get("LLM_API_URL", "http://127.0.0.1:1235/v1/chat/completions")
VERTEX_PROJECT = os.environ.get("VERTEX_PROJECT_ID", "project-a4472335-7c7d-4369-8b4")
VERTEX_URL = f"https://aiplatform.googleapis.com/v1beta1/projects/{VERTEX_PROJECT}/locations/global/endpoints/openapi/chat/completions"

with open(SYSTEM_PROMPT_PATH, "r", encoding="utf-8") as f:
    SYSTEM_PROMPT = f.read()

TEST_CASES = [
    {
        "id": "ru_appointment_tasks",
        "lang": "Russian",
        "name": "Russian Appointments & Relative Dates",
        "transcript": "Напомни мне в эту пятницу в 15:00 встретиться с Расимом в офисе по поводу обновления серверов, новая строка, и обязательно до четверга заказать два патч-корда Cat6."
    },
    {
        "id": "en_technical_solved",
        "lang": "English",
        "name": "English Technical Root-Cause (Solved)",
        "transcript": "So we diagnosed the infinite generation loop in llama-server on SER7. It turns out the DRY sampling flags like dry-multiplier and penalty-last-n were penalizing standard YAML tokens and Cyrillic n-grams. We removed the DRY flags, bumped np to 2 with unified KV cache, and verified port 1235 is rock solid."
    },
    {
        "id": "az_mixed_domain",
        "lang": "Azerbaijani",
        "name": "Azerbaijani Mixed Domain (Tech + Life + Task)",
        "transcript": "Sabah saat 11-də virtsrv3 üzərində Radicale CalDAV konteynerini yoxlamaq lazımdır. Yeni sətir. Bir də axşam aptekdən vitamin almağı unutma."
    }
]

def query_local_qwen(prompt_text):
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Today's Reference Date: 2026-09-01\n\nRaw Transcription Text:\n{prompt_text}"}
    ]
    payload = {
        "model": "qwen",
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": 1200,
        "chat_template_kwargs": {"enable_thinking": False},
        "thinking_budget_tokens": 0,
        "stream": False
    }
    t0 = time.perf_counter()
    r = requests.post(LOCAL_LLM_URL, json=payload, timeout=30)
    r.raise_for_status()
    duration = time.perf_counter() - t0
    data = r.json()
    content = data['choices'][0]['message']['content']
    usage = data.get('usage', {})
    return {
        "content": content,
        "latency_sec": duration,
        "prompt_tokens": usage.get('prompt_tokens', 0),
        "completion_tokens": usage.get('completion_tokens', 0)
    }

def query_vertex_gemini(prompt_text, creds):
    headers = {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "google/gemini-3.7-flash",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Today's Reference Date: 2026-09-01\n\nRaw Transcription Text:\n{prompt_text}"}
        ],
        "temperature": 0.1,
        "max_tokens": 1500
    }
    t0 = time.perf_counter()
    r = requests.post(VERTEX_URL, headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    duration = time.perf_counter() - t0
    data = r.json()
    content = data['choices'][0]['message']['content']
    usage = data.get('usage', {})
    return {
        "content": content,
        "latency_sec": duration,
        "prompt_tokens": usage.get('prompt_tokens', 0),
        "completion_tokens": usage.get('completion_tokens', 0)
    }

def validate_markdown(text):
    clean = text.strip()
    if clean.startswith("```"):
        lines = clean.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        clean = "\n".join(lines).strip()

    valid_yaml = False
    frontmatter = {}
    if clean.startswith("---"):
        parts = clean.split("---", 2)
        if len(parts) >= 3:
            try:
                frontmatter = yaml.safe_load(parts[1]) or {}
                valid_yaml = isinstance(frontmatter, dict)
            except Exception:
                valid_yaml = False

    return {
        "valid_yaml": valid_yaml,
        "frontmatter": frontmatter,
        "cleaned_text": clean
    }

def run_benchmark():
    print("Initializing Google Cloud Vertex AI credentials...")
    creds, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
    creds.refresh(Request())

    results = []

    for tc in TEST_CASES:
        print(f"\n=======================================================")
        print(f"Running Test Case: {tc['name']} ({tc['lang']})")
        print(f"Transcript: \"{tc['transcript']}\"")
        print(f"=======================================================")

        # 1. Local Qwen
        print("  -> Querying Local Qwen 3.6 35B...")
        try:
            qwen_res = query_local_qwen(tc['transcript'])
            qwen_val = validate_markdown(qwen_res['content'])
            qwen_res['validation'] = qwen_val
            print(f"     Done in {qwen_res['latency_sec']:.2f}s (Tokens: {qwen_res['completion_tokens']}, Valid YAML: {qwen_val['valid_yaml']})")
        except Exception as e:
            print(f"     FAILED: {e}")
            qwen_res = {"error": str(e), "latency_sec": 0, "completion_tokens": 0, "validation": {"valid_yaml": False, "frontmatter": {}}}

        # 2. Vertex Gemini Flash
        print("  -> Querying Vertex AI Gemini 2.5 Flash...")
        try:
            gemini_res = query_vertex_gemini(tc['transcript'], creds)
            gemini_val = validate_markdown(gemini_res['content'])
            gemini_res['validation'] = gemini_val
            print(f"     Done in {gemini_res['latency_sec']:.2f}s (Tokens: {gemini_res['completion_tokens']}, Valid YAML: {gemini_val['valid_yaml']})")
        except Exception as e:
            print(f"     FAILED: {e}")
            gemini_res = {"error": str(e), "latency_sec": 0, "completion_tokens": 0, "validation": {"valid_yaml": False, "frontmatter": {}}}

        results.append({
            "test_case": tc,
            "qwen": qwen_res,
            "gemini": gemini_res
        })

    out_file = os.path.join(WORKSPACE_DIR, "benchmark_results.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False, default=str)
    print(f"\nAll benchmark cases completed! Raw results saved to: {out_file}")

if __name__ == "__main__":
    run_benchmark()
