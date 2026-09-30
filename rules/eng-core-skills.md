# Engineering memory trigger

Use the Engineering Continuity Graph when a coding task depends on earlier decisions, constraints, failures, or architecture. Do not replay the whole chat. Conversation is an execution trace, not memory.

At the start of the task, call `ecg_get_session` with the project path. Read the returned memory block if one is present.

- If the block is `<engineering-memory>`, use those fact bodies.
- If the block is `<color/preview>`, the titles are a map only. Call `ecg_search_context` when a title looks relevant. If the titles still do not answer the question, read the source files.
- Say whether the answer came from memory, from source, or from both.

When you learn a durable fact, stage it with `ecg_prepare_placements`. Storage happens only through `ecg_commit_step`, and only after the user grants consent with `ecg_approve_consent`. The approval dialog is the checkpoint. Do not ask a separate "may I remember this?" question in chat.

If the user declines, call `ecg_discard_proposal`. Do not store declined placements. Do not write a summary transcript when a fact fails to store. Do not write a memory entry when nothing matched.

Do not mention tool names to the user.
