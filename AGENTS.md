# OpenCode / Qwen Workspace Rules

This workspace uses a file-based backlog system to queue work for AI agents.

## Workspace Behavior

1. **Backlog Scan on Startup**:
   - At the beginning of the workspace session, automatically scan the `backlog/` directory for any markdown files containing `status: pending` or the tag `#status/pending` in their frontmatter.
   - If any pending backlog notes are found, present them to the user immediately, prioritize them, and ask the user if you should implement them.
2. **Execution**:
   - Implement the requested features or fixes specified in the pending backlog notes.
   - Run tests or verify correctness of code changes.
3. **Closing/Completion**:
   - Once a task from a backlog note is fully implemented and verified, update its YAML frontmatter setting `status: completed` (and tag `#status/completed`).
