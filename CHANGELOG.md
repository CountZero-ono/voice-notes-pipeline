# Changelog

All notable changes to the Voice Notes Pipeline will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [2.4.0] - 2026-09-08

### Changed
- **Bare-Metal HP t630 Cutover:** Migrated 24/7 `voice-notes-worker` daemon and Signal WebSocket ingest from Jasper Lake LXC 107 (`virtsrv2`) to dedicated bare-metal HP t630 Thin Client (`bama`, `192.168.1.37`).
- **90/10 Fast Path Integration:** Verified live sub-3-second end-to-end phone dictation turnaround via Groq Cloud Whisper (~350–650ms) and b.ai Cloud Qwen Flash (~800ms) with Seafile REST vault sync to Obsidian and direct Google Calendar/Tasks push.

## [2.3.0] - 2026-09-06

### Removed
- **Radicale CalDAV Decommissioning:** Retired Radicale CalDAV fallback integration from `voice_harvester.py` and `signal_ingest.py`. Google Calendar & Google Tasks now exclusively handle cloud calendar/task sync, while sovereign Obsidian Markdown files remain the canonical, local source of truth.
- **Brittle CalDAV Plumbing:** Removed raw HTTP PROPFIND requests, manual `.ics` iCalendar string assembly, and unused CalDAV auth configuration.

## [2.2.0] - 2026-09-06

### Added
- **BAMA Unified Inference Gateway Client Integration:** Updated `transcribe_audio()` and `clean_and_extract_llm()` in `voice_harvester.py` to route through `bama-gateway` as the primary Tier 0 24/7 engine (`http://192.168.1.37:8090/v1` with dynamic local probe).
- **Graceful Multi-Layer Resilience:** Verified that if `bama-gateway` is temporarily offline, the pipeline automatically falls back to direct local/cloud cascades (Local Whisper -> Groq Whisper, and Local Qwen -> b.ai -> Vertex).
- **Integration Test Suite Verified:** Validated 4/4 passing integration tests in `tests/test_fallback_cascade.py` under simulated gateway offline conditions.

## [2.1.0] - 2026-09-06

### Added
- **Tier-2 Cloud STT Failover (Groq Whisper):** Added `transcribe_audio_groq()` with `whisper-large-v3-turbo` API failover when local Faster-Whisper CPU inference fails or crashes.
- **Integration Test Suite:** Added `tests/test_fallback_cascade.py` verifying real-world failover cascades across STT (Groq) and LLM (b.ai + Vertex AI) tiers.
- **Local Key Isolation:** Added `.env` loader (`GROQ_API_KEY`) to keep credentials local and sovereign.

### Changed
- **LLM Reasoning Cascade:** Implemented multi-tier fallback from local Qwen 35B (:1235) to b.ai Cloud Qwen (`qwen3.8-flash` with `reasoning_effort: low`) and Vertex AI Gemini 3.7 Flash safety net.
- **Dead-Letter Decoupling:** Decoupled failed LLM notes to `status: dead-letter` instead of `status: pending` to avoid polluting pending appointment queues in Obsidian.
- **Signal Sender Attribution:** Updated message handling to prioritize envelope `source` for accurate reply routing.

### Fixed
- **Google Calendar All-Day Event Contract:** Enforced exclusive `date + 1 day` end dates for all-day events per Google Calendar API requirements.
- **Infinite Audio Retry Loop:** Capped unparseable audio file retry attempts to 3 before moving to dead-letter storage, eliminating the 5-second infinite loop bug.
- **b.ai Latency Optimization:** Injected `chat_template_kwargs: {reasoning_effort: "low"}` to eliminate 30s timeouts on Cloud Qwen.
