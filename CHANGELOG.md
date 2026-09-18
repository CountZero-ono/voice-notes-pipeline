# Changelog

All notable changes to the Voice Notes Pipeline will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [2.5.3] - 2026-09-19

### Fixed
- **Seafile URL Path Quoting:** Fixed HTTP 400 silent upload failures in `seafile_sync.py` by applying `urllib.parse.quote(parent_dir, safe='/')` in `get_upload_link()` and `get_update_link()` for nested directory paths.
- **Credential Hygiene:** Removed hardcoded default Seafile token and repo ID fallbacks in `seafile_sync.py`, strictly enforcing environment configuration.
- **OpenRouter Reasoning Control:** Added `"reasoning": {"effort": "none"}` and fallback extraction for `choice.message.reasoning` in `extract_llm_cloud_openrouter()`, eliminating token cutoffs and reducing Tier-2 failover latency to <1s.
- **Cascade Unit Test Isolation:** Updated `tests/test_fallback_cascade.py` to mock `get_gateway_url` and isolate `OPENROUTER_API_URL`, ensuring all 4 failover tiers (Groq STT, OpenRouter LLM, Vertex AI LLM, End-to-End note generation) execute and verify deterministically without hitting live gateway endpoints.

## [2.5.2] - 2026-09-18

### Changed
- **OpenRouter Tier-2 Failover Migration:** Migrated Tier-2 cloud extraction fallback from deprecated b.ai API to OpenRouter (`https://openrouter.ai/api/v1/chat/completions`) using `qwen/qwen3.8-flash`.
- **Dynamic Credential Discovery:** Added multi-path resolution in `get_openrouter_api_key()` checking environment (`OPENROUTER_API_KEY`), local `.env`, `~/.hermes/.env`, and `~/Documents/openrouter.txt`.
- **Backward Compatibility:** Preserved `get_bai_api_key`, `extract_llm_cloud_qwen`, `BAI_API_URL`, and `BAI_MODEL` aliases for existing cascade callers and tests.

## [2.5.1] - 2026-09-17

### Added
- **Invocational Lead Rule for Agent Call to Action:**
  - Standardized in `system_prompt.md` and `voice_harvester.py` that voice notes are classified as `agent` (Call to Action $\rightarrow$ `AgentBacklog`) if and only if the speaker directly addresses an agent by name at the beginning of the message (allowing for natural fillers like "Hey", "Эй", "Так", "Ну").
  - Target names supported in Latin and Cyrillic: `Gemini`, `Antigravity`, `Claude`, `Agent`, `Dixie`, `Qwen`/`Gwen` (`Джемини`, `Гемини`, `Дикси`, `Антигравити`, `Клод`, `Агент`, `Квен`).
  - Added deterministic regex pre-check (`AGENT_INVOCATION_REGEX`) and frontmatter `tags` inspector in `voice_harvester.parse_categories_from_llm()`.
  - Passive mentions of agents in the middle/end of sentences remain as context in `technical` or `life`.
  - Enforced exclusivity: if `agent` is active, `life` is stripped from categories so action items are never routed into `Life/`.
  - Added unit test suite covering invocational leads, fillers, passive mentions, and manual tag overrides (13/13 tests passing).

### Fixed
- **Signal Audio Dispatch Target Name:** Fixed `NameError: name 'process_audio_attachment_sync' is not defined` in `signal_ingest.py` by pointing thread pool dispatch to `process_incoming_voice_note`.

## [2.5.0] - 2026-09-11

### Added
- **Option 1 Autonomous Agent Queue (`AgentBacklog`):**
  - Added `agent` category to `system_prompt.md` and whitelist in `voice_harvester.py` for voice notes intended for AI coding assistants/agents ("Gemini", "Antigravity", "Claude", etc.).
  - Added dedicated routing to `/VoiceNotes/Inbox/AgentBacklog/` with frontmatter `status: pending` and tag `agent-backlog`.
  - Added Seafile REST sync for `AgentBacklog` notes directly into remote Seafile vault.
  - Added distinct Signal completion alert: `🤖 Agent Task Staged in AgentBacklog! (Status: pending)` with extracted target and action summary.
  - Updated `.antigravity.md` workspace behavior to proactively sweep `/home/fuad/Seafile/Obsidian Vaults/VoiceNotes/Inbox/AgentBacklog/` for pending tasks on session startup.

### Fixed
- **Language Extraction Enforcement:** Enforced strict language boundary rules in `system_prompt.md`. While `# Cleaned Transcript` preserves the verbatim spoken tongue (Russian, Azerbaijani, English), all extracted summaries, tasks, problem statements, and key facts are now strictly forced into technical English across all model tiers.
- **BAMA Gateway Local-First Routing:** Switched `LLM_PRIMARY=local` on `bama-gateway` (HP t630, `192.168.1.37`), prioritizing sovereign SER7 Local Qwen 35B whenever the workstation is online with ultra-fast 250ms TCP failover to b.ai Cloud Qwen / Vertex Gemini when SER7 is powered off.

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
