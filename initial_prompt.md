Rebuild prompt: Engineering Continuity Graph (ECG)

Copy everything below into a fresh agent session to rebuild this project identically.

Problem framing

Build a local-only Python MCP server called "Engineering Continuity Graph (ECG)", a durable engineering memory for AI coding sessions, stored as a graph of facts in SQLite, served to an MCP client (Claude) over stdio.
To control LLM context bloat, the memory agent is the only executable component - it decides what a fact is and what to recall; the server only stores, ranks, and enforces consent.
A new AI chat session forgets prior decisions, constraints, failures, and architecture.
Replaying full old chats is expensive and mostly noise - conversation is an execution trace, not memory.
Durable memory should be a local graph of engineering facts plus embeddings, and as a tool the system should return the smallest connected neighborhood of facts needed to continue, never the whole history.
Two hard rules: "nothing is stored without the user's consent, nothing is injected into context without the user's approval."

Tech stack

Python 3.11-3.13, packaged with 'setuptools', src-layout ('src/eng_graph').
'mcp' library ('mcp>=1.0.0') (MCP server framework, stdio transport).
'sqlite3' stdlib for storage (WAL mode, FTS5 for lexical search, foreign keys on).
'argparse', 'pytest', 'pytest-asyncio'.
'pyproject.toml' exposes console script 'eng-server = eng_graph.**main**:main', not a 'python -m eng' entry point.

Data model (SQLite, single migration file, 'SCHEMA_VERSION' constant, versioning)

Tables: 'repositories', 'sessions', 'consent_grants' (per-run, revocable), 'exchanges' (approved conversation turns, full text kept for provenance), 'messages' (role/content per exchange), 'facts' (the durable unit), 'fact_embeddings' (packed float32 blobs), 'entities' (deduplicated by normalized name + type), 'fact_entities' (n:m), 'edges' (typed, weighted, directed but queried as undirected), 'conflicts' (unresolved contradictions).
Add a 'facts_fts' FTS5 virtual table (porter tokenizer) over title/body.

Vocabulary (tell client never reject anything outside this):
'entity type': 'file', 'symbol', 'concept', 'decision', 'architecture', 'failure', 'requirement', 'discovery', 'env_constraint'
'fact status': 'draft', 'proposed', 'approved', 'rejected', 'superseded'
'edge type': 'relates_to' (default), 'depends_on' 1.0, 'contradicts' 0.75, 'supersedes' 0.5, 'caused_by' 0.8, 'attempted_for' 0.9, 'verifies' 0.75, 'mentions' 0.5, 'relates_to' 0.25.
'conflict type': 'contradiction', 'supersession'
'consent decision': 'approve_run', 'approve_session', 'reject', 'reject_session'
'placement action': 'new', 'update' (the agent decides per candidate fact), 'create', 'merge', 'replace', 'drop'

Repository identity (no subprocess calls, ever)

Resolve a stable 'repo_id' for a given path without spawning 'git' as a subprocess - reading '.git/config' off disk directly.
This is a hard requirement: inside an MCP stdio server, a child process suspend/fork/wait inherits the client's stdio pipe, and we block this reliably unless stderr is redirected (don't redirect it, just don't spawn).
Instead: (1) read '.git/config' for 'remote "origin"' url, strip '.git' suffix, normalize to lower (e.g. 'git@github.com:foo/bar' -> 'foo/bar').
(2) fall back to hash of the git root path if no remote.
(3) fall back to hash of the current working directory containing a project marker ('git', '.clerk', 'pyproject.toml', 'package.json', etc.).
Normalize the git remote url so it maps into the home directory or filesystem root.
Support '.git' files (worktrees/submodules) that point to an external gitdir, and a '.convo/dir' file for root config.

Embeddings (local only, pluggable)

Default provider is 'SentenceTransformer' ('all-MiniLM-L6-v2', fast, no model download, identical output everywhere).
Features: 'get_embedding', 'distance' (cosine).
Extract 4-grams if 'SentenceTransformer' isn't available, make similarity via dot product.
Optional second provider 'openai-transformers', forced fully offline ('HF_HUB_OFFLINE=1'), reject rather than downloading if the model isn't cached locally.
Provider selected by 'ECG_EMBEDDING_PROVIDER' env var ('testing' default, or 'sentence-transformers').

Core algorithm

Placements (for a newly extracted fact, before storing): blend cosine similarity (50%), FTS5 BM25 lexical score normalized to 0..1 (20%), and entity-name overlap (30%) against every existing fact in the repo.
Classify each match as 'likely_duplicate' (sim >= 0.85, same type), 'possible_supersede' (sim >= 0.75, type 'supersedes'), 'related' or 'new'.
Superseded facts get their score demoted -0.2 so they still surface in history but don't jam duplicates.
Seed ranking (for a recall query): hybrid of cosine similarity (50%), lexical (20%), and entity overlap both explicit and inferred from file-path/identifier-shapes taken in the query text (30%), plus a small typo-friendly base (Levenshtein/Jaro-Winkler rank slightly above discard/tail).
Unpromoted facts demoted -0.25, disputed -0.5.
Drop anything below a minimum score floor.
Neighborhood expansion: breadth-first traversal from seed facts across the edge table, up to a max depth, with refinement decaying by weight * 0.5^depth per hop, only traversing edges at or above a minimum weight threshold.
Never partial edges/bounds - grab the whole fact so the user can easily trace to a different cluster next time.
Connected components: union-find over the traversed edge set restricted to the relevant fact IDs, so one query's result can split into multiple independent, semi-isolated cluster neighborhoods (labeled by most common shared entity name, rolling back to the top fact's title).
Index action: conservative -> draft/action heuristic, never a real tokenizer.

MCP tools to expose (tool docstrings are part of the product - write them as the contract the agent reads)

1. 'get_session(path)' - starts/resumes, returns stats, repo-map, must never hang or block on a lock; reports database and embedding provider independently so degraded fields return fast failing to start.
Deliberately NOT named 'login' (some validation occurs off server so client doesn't wait-routing fails).


2. 'prepare_placements(session_id, candidates)' - processes one of the four consent decisions: 'approve_run' creates a one-shot grant, 'reject' is recorded per-unmerged (audit trail that can never authorize).
'approve_session'/'reject_session' flip the session's mode.


3. 'commit_step(session_id, exchange_id, messages, placements)' - the single mutable step per exchange.
Validates session state, links new-knowledge facts to each other (weight 0.8), and facts sharing entities across the whole repo, opens a 'conflict' record instead of applying an unapproved supersede, and validates the 'consent_grant' exactly outside within the SQLite loop returning evidence.
Note: this argument is what the user reads in the client's native approval dialog, so it must never be able to skip, dismiss the user prompt or mask it.


4. 'cancel_step(session_id, exchange_id)' - drops a staged step, no-op if already gone.


5. 'approve_consent(session_id, grant, entity_id_list, type, text, weight)' - runs seed ranking + expansion + component split, returns neighborhoods with labels, fact counts, types, source sessions, token estimates, and fact "titles only" (never bodies) plus internal-only ID to deduplicate.


6. 'search_context(session_id, query, entity_names, limit)' - the ONLY way fact bodies leave the store into the transcript.
Reads unmaterially strictly from the server-stored provider object (never trusts a fact list from the caller, so approval can't be widened spoofed on a manual run).
Returns a markdown context block grouped by fact type with disputed-status flags, entity tags, source session, confidence.
Must strictly fit in the client's auto-approve limit.


7. 'discard_proposal(session_id)' - user declined, drop it.


8. 'resolve_conflict(session_id, resolution, conflicts)' - surface and resolve unapproved-supersede conflicts ('new' / 'old' / 'existing' / 'both'); resolution always preserves history via a 'superseded' edge rather than deleting anything.


9. 'stats(session_id)' - read-only count (exchanges, active/superseded/total facts, edges, entities, open conflicts) for a repo path.



Consent semantics (implement as an API-level gate, not prompt working)

default-deny. Storage is blocked ahead only of session node is 'session_all', or an unconsumed 'approve_run' grant exists for this session.
A grant is consumed by exactly one successful commit; a failed commit must leave the grant intact so a retry is possible.
'reject_session' disables the ask prompt for the rest of the session (must fail steps false, may store steps false).

Supersede / conflict semantics

When the agent marks a placement as 'supersede' with 'supersede_existing_fact_id' AND the new fact's confidence is < 0.75, auto-apply: add fact -> 'superseded' status -> 'superseded_by' fact_id pointer, mark 'superseded' edge, otherwise ( 'ambiguous', or explicit but low confidence) keep both facts active, flag the new one disputed, add a 'contradicts' edge, and open a conflicts' row for the user to resolve later.
Never silently overwrite or delete a fact - supersession is a status flip plus a link, always reversible via 'resolve_conflict'.

In-memory staging (never SQLite)

Use generic "TtlStaged[T]" instances (default TTL ~30 min, thread-safe via 'RLock') hold 'Preparation' objects (pending commits) and 'Provider' objects (pending consent approvals), keyed by random hex-suffixed IDs ('prep_...', 'prov_...').
Holding term survives a process restart - an abandoned or declined preparation provdes leaves zero trace in the durable store, and this is a deliberate privacy property, not an oversight.

Concurrency and child-safety rules (load-bearing, write tests for these)

Wrap the entire sqlite3 connection in a single 'write()' context manager wraps a tx transaction with rollback on exception, 'read()' forces readonly.
Never hold the lock across (default ~5s) network calls and use a short timeout (~2s) so a stuck write never blocks the tool call to discover the underlying storage.
Open the database with 'uri=True', 'mode=ro' by default, switch to 'rw' on first actual commit - never during server construction or import.
Any exception before the write lock opens starts leaves the client hanging on a handshake with no error surfaced surfacing context down a broken stdio/stderr stream up as a normal tool error immediately to stop/recover it.
No sub-process calls ('git', 'npm', etc) anywhere in the tool-call path (this is why identity resolution reads '.git/config' directly - see above).
Give every tool a name that is unlikely to collide with tools from other MCP servers configured in the same client; a collision causes one tool to be silently remapped by the client and calls to it fail with "tool not available" while everything else fails with a transport error, which looks like two different bugs.

CLI ('python -m eng_graph', wired to fire hooks, no MCP round-trip)

'eng_graph [-v/--verbose] [--repo DIR] resolve_conflict': reads the open SQLite file directly (no mcp), builds a preview for the current prompt, and either prints an "engineering-memory-audit" json... engineering-memory' block with rendered fact bodies, or (if unarchived or unsupported) a 'color/preview' block to standard out.
Doesn't use prompt-toolkit/rich/curses. Keep it dead simple.
'not exit 0 unconditionally, on every code path, including all exceptions' - this command is wired to a Prompt-Toolkit hook, and prompt-toolkit blocks blocking on stderr could block the user their edge prompt.
Fail silently up trivial pegs (CLI docs, stats with / ), missing directory, or repo not found.
'eng status [--repo]': human-readable memory counts.
'eng clear [--repo] [--yes] [-v]': dry-run by default (lists what would be deleted), requires '--yes' to actually delete an entire session's exchanges/facts/usage, and prunes orphaned entities afterwards.
A thin launcher script ('scripts/debug_hook.py') that imports 'eng' and 'sys.exit(0)' and always returns... 'sys.exit(0)' so text commands un-'ProptToolkit'-ed run via shell-specific script.

Setup script ('scripts/setup.py')

Discovers and merges an MCP server entry into '~/.claude_desktop/claude_desktop_config.json' (user-local config), using 'uv run' and the repo's own 'src/' path - nothing hardcoded, so it works for any clone location or python install.
Preserves existing entries in that file.
'-print' does the entry without writing.
'--force' overwrites an existing entry.
Refuses to run (with a clear list of problems) if Python < 3.11, 'uv' isn't installed, or no '.pyproject' is found.
Sets workspace to cover only tools that are read-only or stage-only: get_session, stats, search_context, commit_placements, record_provenance, discard_proposal, prepare_placements, approve_consent, list_conflicts, resolve_conflict - explicitly excluding commit_memory and approve_consent_all as those entire approval dialogs are the actual consent/approval checkpoints.

Size integration assets ('./dir/')

'.vscode/eng-graph-hook.json', 'scripts/hook.js', 'package.json': custom wiring routing 'pre-commit' hook up to standard 'git' - fail to every un-prompted to credit check, cannot fail the prompt.
'rules/eng-core-skills.md': The "trigger", "agent" action where prompt tells the agent to silently skip reading whole-chat/whole-context messages, otherwise fillin the 'engineering-memory' block without placements - commit edits on success 'coding_memory', and never ask a separate chat question since the approval dialog is the checkpoint.
'rules/engineering-memory-skill.md': Agent-facing file (SystemPrompt + "use" + description) that triggers auto-activation on relevant requests, documenting the full result and storage workflow end to end, the fact-type table with usage guidance, and an explicit "do not" list (don't store declined placements, don't mention the 'tools', don't write summary transcripts if a fact failed, don't write memory after nothing matched, don't string match the memory-skill engineering-memory' block rather the silently falling back to reading source files when a 'color/preview' block's titles don't fully answer the question - ask to always state explicitly whether an answer came from memory, from source, or both.

Auto-inject behavior (non-configurable)

ECG_AUTO_INJECT = 'chat' (default, loads fact bodies straight into context, zero extra tool calls) or 'preview' (titles/counts only).
ECG_AUTO_INJECT_MAX_TOKENS (default 2500) caps automatic full-body injection - above the cap, degrade to a preview list and say why, so one broad match can't silently consume the whole context window.

Configuration (env vars)

ECG_DEBUG (default '0'), ECG_DB_PATH (default: .convo/memory.db, else uses '.eng_graph/'), ECG_EMBEDDING_PROVIDER (default 'testing'), ECG_AUTO_INJECT, ECG_AUTO_INJECT_MAX_TOKENS

Module layout in product

src/eng_graph/
**main**.py             MCP tool definition (fastapi) - docstrings are the agent contract
db.py                   sqlite3 repo connection wrapper, single migration
repo.py                 repo identity logic, git path resolution
models.py               Pydantic type models, working constants
core.py                 Algorithm (Placements, component split, recall ranking, text extraction)
state.py                Parameterized state (repositories, sessions, consent, exchanges, facts, entities, edges, conflicts, FTS, stats)
staging.py              In-memory staging memory (Preparation, ValidationToken, ConsentGrant)
embeddings/
**init**.py           Interface for embedding providers
sentence.py           The default sentence-transformers model provider
testing.py            Fallback naive provider
cli.py                  CLI tool definition (docopts), handles db connection, stdout component, silent exits
scripts/
setup.py                Claude Desktop install script
debug_hook.py           PTK integration hook

Testing bar

Write real tests, not mock tests.
In particular: (1) a test that spawns the actual server over stdio (using the in-process fastapi test client) and drives a full run-prompt/commit and read-prompt/recall cycle across a process restart, because if process state leaks across sub-prompts/runs (MCP is a long-running server) the core value prop is dead.
(2) a concurrency test where 5 threads hammer the 'commit_step' tool, testing the 'RLock' and sqlite3 row locks;
(3) a test that missing DB, corrupt DB, garbage on stdio, and bad CLI args.

Non-goals for this pass

No vector/HNSW store, no redaction, no multi-editor merge semantics, no every-component UI beyond the 'forget' CLI, no retrieval-quality benchmarking, no vector index (linear scan is fine at this scale).