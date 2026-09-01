# Voice Notes Pipeline – Code Review

Project: `/home/fuad/Projects/BAMA/voice-notes-pipeline`
Files reviewed: `voice_harvester.py` (835 L), `signal_ingest.py` (666 L), `tests/test_e2e.py`, `system_prompt.md`.

## Priority 1 – Security / secrets leakage

- `voice_harvester.py:44-45,83-86` – Seafile token, Radicale user/password defaulted to real credentials in source. Same for `SEAFILE_TOKEN` default.
- `signal_ingest.py:34` – `SIGNAL_PHONE_NUMBER` defaulted in code.
- Action: move all secrets to env-only, fail fast if missing, never commit defaults. Rotate exposed tokens.

## Priority 1 – Async robustness / blocking event loop

- `signal_ingest.py:645-666` – `listen_signal_websocket` is async, but `process_signal_envelope` is completely synchronous and does network I/O, file writes, LLM calls, transcription. Invoked directly from the websocket loop, so one slow transcribe/LLM request blocks the whole Signal stream.
- `signal_ingest.py:112-127, 620-638` – `send_split_signal_message` sleeps `time.sleep(0.5)` and `send_signal_message` does blocking `requests.post`. Called from the same sync path.
- `signal_ingest.py:448-451, 492-495, 542-545` – voice note handling in Antigravity/Dixie/Qwen groups calls `voice_harvester.transcribe_audio` synchronously.
- Action: isolate heavy work to a `ThreadPoolExecutor` / `asyncio.to_thread`, or move processing to a queue worker. Use aiohttp for Signal API calls. Add semaphore to cap concurrent transcriptions/LLM calls.

## Priority 1 – Data loss / race conditions

- `voice_harvester.py:636-728` – `check_and_sync_approved_notes` walks `INBOX_DIR`, reads markdown files, mutates front-matter in memory and rewrites the file. No file lock, no atomic write. Concurrent writes can tear files.
- `voice_harvester.py:720-724` – status is changed to `synced` *before* verifying that Radicale/Google pushes succeeded. On push failure the note is already marked synced and will never retry.
- `voice_harvester.py:373-376, 383-386, 409-411` – `handle_text_command` does in-place string replace on the note file with no lock and no validation of front-matter structure.
- `voice_harvester.py:131-132` – `save_state` writes JSON without temp-file + rename. Power loss can corrupt `processed_files.json`.
- Action: write files via `tmp -> os.replace`, use `filelock` or `fcntl` for note updates, only flip status after successful push, make state save atomic.

## Priority 2 – Error handling & resilience

- `voice_harvester.py:97-117` – `load_whisper` calls `sys.exit(1)` on import or init failure. Kills daemon instead of retrying.
- `voice_harvester.py:134-149` – `wait_for_file_to_stabilize` swallows `OSError` and returns `False` on timeout, but `monitor_loop` just continues – no back-off and no logging of final size.
- `voice_harvester.py:239-248` – LLM request has no retry, no back-off, no timeout jitter. Returns `None` on any exception, `process_file` then aborts and leaves raw file unarchived.
- `voice_harvester.py:47-74` – `upload_to_seafile` returns `False` on any error but caller never retries; note is still written locally.
- `voice_harvester.py:341-400` – `push_event_to_radicale` builds iCal manually. No validation of `start_time` format, no retry, no check for HTTP errors beyond `raise_for_status`. All-day `DTEND` uses `end_dt.strftime("%Y%m%d")` which is correct but `DTSTART` line is built from `date_clean` without timezone – fragile.
- `signal_ingest.py:268-297` – `check_calendar_conflicts` hard-codes `http://192.168.1.30:5232` when constructing `event_url`, ignoring `RADICALE_CALENDAR_URL`. PROPFIND parsing is regex based and fetches every `.ics` – O(N) and fragile.
- Action: add retry with exponential back-off for all external calls, differentiate transient vs permanent errors, log request IDs, surface errors to Signal user.

## Priority 2 – Parsing fragility

- `voice_harvester.py:250-290` – `parse_categories_from_llm` splits on `---` and assumes front-matter exists. If LLM returns markdown with `---` in body it breaks. Inline array parsing is manual.
- `voice_harvester.py:292-322` – `parse_event_from_frontmatter` splits on first `:` only, no multiline support, no type validation.
- `voice_harvester.py:546-634` – `write_to_inbox` rebuilds YAML manually. Bug at line 604: when rebuilding `categories`, `copies`, `tags` the list items for `tags` is rebuilt correctly but the generic loop at 601-608 writes `"{k}: \"{v}\""` for scalar values, which will break if value contains `"`. Also `yaml_data["categories"]` is overwritten, losing original ordering.
- `signal_ingest.py:336-357` – `parse_note_details` uses same fragile split.
- Action: use a real YAML parser (`yaml.safe_load/dump`) for front-matter, validate schema with pydantic, never hand-roll.

## Priority 2 – Operational issues

- Logging: `voice_harvester.py:22-29` and `signal_ingest.py:24-31` both configure `logging.basicConfig` writing to the same `harvester.log` with no rotation. Log file will grow unbounded.
- `voice_harvester.py:775-819` – monitor loop sleeps 5 s, scans whole `RAW_DIR` each iteration, walks `INBOX_DIR` for sync on every loop. No debouncing, no inotify.
- State keyed only by filename `state[filename]` – collisions if same name appears in subfolders or file is recreated.
- `archive_file` moves file after successful processing but does not verify archive write succeeded before removing source.
- DRY_RUN mock in `clean_and_extract_llm` uses keyword matching; real LLM failures are silent.

## Priority 3 – Edge cases

- Empty transcript: `process_file` checks `if not raw_transcript.strip()` and returns `False`, but file is not archived, will be retried forever.
- Large audio: no size limit before `transcribe_audio`; `faster-whisper` loads whole file into memory.
- `voice_harvester.py:358-369` – `push_event_to_radicale` builds `start_clean` with `start_time.replace(':','')` without validating length – `15:30` → `153000` ok, `9:5` → malformed.
- `push_task_to_radicale` alarm `TRIGGER;VALUE=DATE-TIME` uses `date_clean` without time zone – may be invalid.
- `signal_ingest.py:73-81` – `format_signal_group_id` base64-encodes group id with `utf-8`, but Signal group ids are base64url without padding; mismatch will break messaging.
- `send_signal_message` builds recipients list but never validates `SIGNAL_API_URL` reachability.
- `query_qwen` mutates history list in place without lock – concurrent Signal messages corrupt history.

## Priority 3 – Testability / maintainability

- `tests/test_e2e.py` relies on a real audio fixture and `voice_harvester.process_file`. No mocking of Whisper/LLM/Radicale. Test will fail in CI without model and network.
- No unit tests for parsers, front-matter handling, state management.
- `voice_harvester.py` is 835 lines, multiple responsibilities: transcription, LLM, file IO, CalDAV, Seafile, Google APIs. Hard to test.

## Immediate action items

1. Remove hardcoded secrets, enforce env vars, rotate tokens.
2. Make `check_and_sync_approved_notes` and note updates atomic with file locks and write-temp-rename; only mark `synced` after successful pushes.
3. Offload heavy work from Signal websocket loop to a worker pool / queue; make Signal handlers async/non-blocking.
4. Replace hand-rolled YAML parsing with `pyyaml` and schema validation; fix `write_to_inbox` YAML rebuild bug.
5. Add retry with back-off for LLM, Seafile, Radicale, Google APIs; surface transient failures.
6. Add log rotation and structured logging.
7. Fix `check_calendar_conflicts` hardcoded IP and naive conflict detection.
8. Add size/time limits for audio transcription, validate time formats before building iCal.
9. Harden state persistence with atomic writes and filename+path keys.
10. Expand tests with mocks for Whisper, LLM, CalDAV; add unit tests for parsers and front-matter.
