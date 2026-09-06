# Changelog

All notable changes to the Voice Notes Pipeline will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

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
