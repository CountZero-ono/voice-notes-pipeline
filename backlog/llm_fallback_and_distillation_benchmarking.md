---
title: "3-Tier Fallback Architecture & Distillation Benchmarking (Groq, Vertex Gemini, Offline Queue)"
status: pending
tags: ["#voice-notes-pipeline", "#status/pending", "#distillation", "#fallback", "#groq", "#gemini", "#offline-queue"]
date_created: 2026-08-26
---

## Objective
Implement a multi-tier, high-availability architecture for `voice-notes-pipeline` (and downstream `prompt_distiller`), benchmark Vertex AI Gemini Flash and Groq Whisper against local Qwen 35B / Whisper, and establish a durable offline retry queue.

---

## 3-Tier Fallback Architecture

1. **Tier 1 (Primary - Local Zero-Cloud):**
   - **STT:** Local `faster-whisper` (`large-v3-turbo` on CPU).
   - **Extraction & Structuring:** Local Qwen 3.6 35B (`http://127.0.0.1:1235`).
   - **Calendar/Tasks:** Google Calendar/Tasks $\rightarrow$ Radicale CalDAV fallback.

2. **Tier 2 (Cloud Failover - Instant Processing when Local AI is busy or down):**
   - **STT:** Groq Cloud Whisper API (Free tier: 2,000 req/day, sub-second transcription) OR direct multimodal audio to Vertex Gemini Flash.
   - **Extraction & Structuring:** Vertex AI Gemini Flash.
   - Triggers automatically if local Whisper hangs or local Qwen endpoint times out after retries.

3. **Tier 3 (Zero Connectivity / Offline Queue):**
   - If PC is powered off, asleep, or network is completely disconnected:
     - Audio clips remain safely queued in the Seafile mobile sync folder or local `staging/queue/`.
     - Upon workstation wake-up or connection restoration, the queue daemon wakes up, drains clips in FIFO order, processes them through Tier 1 or Tier 2, and sends delayed completion receipts back to Signal.

---

## Progress & Status (Updated 2026-09-01)
- [x] **Tier 1 Optimization:** Removed DRY sampling flags from `llama-qwen-mtp.service` and upgraded to `-np 2` slots with unified KV cache.
- [x] **Tier 2 Integration:** Integrated Vertex AI Gemini 3.7 Flash cloud failover into `voice_harvester.py`.
- [x] **Tier 3 Dead-Letter:** Implemented raw Whisper transcript fallback to `Inbox/Life/` with `#dead-letter` if all LLMs are unreachable.
- [x] **Distillation Shootout:** Completed trilingual benchmark between Local Qwen 35B and Vertex AI Gemini 3.7 Flash (`tests/benchmark_distillation.py`). Results documented in `distillation_benchmark_report.md`.

---

## Remaining Tasks for Next Session
1. **Groq Whisper STT Failover:**
   - Integrate Groq Cloud Whisper API fallback into `voice_harvester.py` if local `faster-whisper` fails or hangs.
2. **Tier 3 Persistent Queue:**
   - Implement persistent queue auto-drain loop on workstation wake-up.
3. **Downstream Rollout (Prompt Distiller):**
   - Port the dual-provider LLM fallback pattern into `prompt_distiller`.
