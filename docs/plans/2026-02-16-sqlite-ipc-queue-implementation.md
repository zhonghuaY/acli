# SQLite IPC Queue Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace single-slot file input delivery with SQLite-backed queued delivery so waiting Agent receives messages reliably in FIFO order.

**Architecture:** Keep existing process model (`wait_input.py` + `SessionFiles`) but add a SQLite queue/state store under IPC runtime dir. `send_input()` enqueues messages; `wait_input.py` dequeues atomically. Marker files remain for compatibility/diagnostics.

**Tech Stack:** Python stdlib (`sqlite3`, `subprocess`, `unittest`), existing `acli` modules.

---

### Task 1: Lock behavior with failing tests

**Files:**
- Modify: `tests/test_regressions.py`

**Step 1: Write failing test**
- Add FIFO regression test for two queued messages in same session.

**Step 2: Run test to verify failure**
- Run: `python3 -m unittest tests.test_regressions.WaitInputRegressionTests.test_sqlite_backend_preserves_fifo_for_multiple_messages -v`
- Expected: FAIL (`M1` not found / timeout on second read).

### Task 2: Add SQLite IPC primitives

**Files:**
- Modify: `ipc.py`

**Step 1: Add SQLite backend config and DB initialization**
- Add backend switch (`sqlite`/`file`), DB path, schema for `messages` and `waiting`.

**Step 2: Add queue APIs**
- `enqueue_input(session_id, content)`
- `dequeue_input(session_id)` (atomic FIFO consume)
- `set_waiting/clear_waiting/is_waiting(session_id)` with stale PID cleanup.

**Step 3: Wire `SessionFiles`**
- `send_input()` routes to SQLite queue when backend is `sqlite`; keeps file behavior for `file`.
- `is_waiting` checks SQLite waiting state first in sqlite mode and keeps marker fallback.
- `cleanup()` removes session queue state.

### Task 3: Update wait loop to consume queue

**Files:**
- Modify: `wait_input.py`

**Step 1: Add backend-aware receive path**
- In sqlite mode, call queue dequeue in polling loop.
- Keep timeout and marker lifecycle behavior intact.

**Step 2: Keep backward compatibility**
- Preserve existing file mode path and logs.

### Task 4: Verify and harden

**Files:**
- Modify: `tests/test_regressions.py` (if needed for env isolation)
- Modify: `README.md` (env var docs)

**Step 1: Run targeted tests**
- `python3 -m unittest tests/test_regressions.py -v`

**Step 2: Run syntax check**
- `python3 -m py_compile acli.py agent_process.py config.py ipc.py repl.py session_manager.py wait_input.py tests/test_regressions.py`

**Step 3: Update docs**
- Document `ACLI_IPC_BACKEND` and sqlite default behavior.
