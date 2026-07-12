---
title: "Configure Round Sync on Phone for WebDAV"
status: pending
tags: ["#voice-notes-pipeline", "#status/pending"]
---
Configure Round Sync on the Android phone to sync voice recordings with Seafile via WebDAV:
1. Connect Round Sync to Seafile WebDAV (`https://seafile.yourdomain.com/seafdav`).
2. Create a task to sync the phone's voice recordings directory to `VoiceNotes/Raw/`.
3. Set task schedule to periodic sync (e.g., every 5-10 minutes).
4. Disable battery optimization (set battery usage to "Unrestricted" for Round Sync) in Android settings to ensure the background task runs reliably.
