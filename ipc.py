"""
acli IPC — 基于 SQLite 队列 + 文件流的进程间通信

文件约定 (全部在 ~/.acli/ipc/ 下):
  acli_out_{session_id}.jsonl      Agent CLI 输出 (stream-json)
  acli_err_{session_id}.err        Agent CLI stderr
  acli_prompt_{session_id}.txt     首轮 prompt (cat 方式喂给 agent)
  acli_waiting_{session_id}.marker wait_input.py 就绪标记
  acli_ipc.sqlite3                 输入队列 + waiting_state
"""
from __future__ import annotations

import glob
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Generator, Optional

from config import (
    IPC_DIR,
    IPC_PREFIX,
    POLL_FAST,
    POLL_SLOW,
    WAIT_INPUT_AFTER_RESULT_GRACE_SECS,
)

logger = logging.getLogger("acli.ipc")

IPC_DIR.mkdir(parents=True, exist_ok=True)
SQLITE_DB_FILE = IPC_DIR / f"{IPC_PREFIX}ipc.sqlite3"
_DB_READY = False


def _db_connect() -> sqlite3.Connection:
    logger.debug("open sqlite connection: file=%s", SQLITE_DB_FILE)
    conn = sqlite3.connect(str(SQLITE_DB_FILE), timeout=30.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _ensure_db() -> None:
    global _DB_READY
    if _DB_READY:
        return
    logger.info("initializing sqlite ipc db: file=%s", SQLITE_DB_FILE)
    with _db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS input_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_input_queue_session_id
            ON input_queue(session_id, id)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS waiting_state (
                session_id TEXT PRIMARY KEY,
                pid INTEGER NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
    _DB_READY = True
    logger.info("sqlite ipc db ready: file=%s", SQLITE_DB_FILE)


def enqueue_input(session_id: str, content: str) -> None:
    _ensure_db()
    with _db_connect() as conn:
        conn.execute(
            "INSERT INTO input_queue(session_id, content, created_at) VALUES(?, ?, ?)",
            (session_id, content, time.time()),
        )
    logger.debug("[%s] enqueue_input ok: bytes=%s", session_id, len(content or ""))


def dequeue_input(session_id: str) -> Optional[str]:
    _ensure_db()
    conn = _db_connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id, content FROM input_queue WHERE session_id = ? ORDER BY id LIMIT 1",
            (session_id,),
        ).fetchone()
        if not row:
            conn.execute("COMMIT")
            logger.debug("[%s] dequeue_input empty", session_id)
            return None
        msg_id, content = row
        conn.execute("DELETE FROM input_queue WHERE id = ?", (msg_id,))
        conn.execute("COMMIT")
        logger.debug("[%s] dequeue_input ok: msg_id=%s bytes=%s", session_id, msg_id, len(content or ""))
        return content
    except Exception as e:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        logger.error("[%s] dequeue_input failed: %s", session_id, e)
        raise
    finally:
        conn.close()


def set_waiting_state(session_id: str, pid: int) -> None:
    _ensure_db()
    with _db_connect() as conn:
        conn.execute(
            """
            INSERT INTO waiting_state(session_id, pid, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                pid = excluded.pid,
                updated_at = excluded.updated_at
            """,
            (session_id, pid, time.time()),
        )
    logger.debug("[%s] set_waiting_state: pid=%s", session_id, pid)


def clear_waiting_state(session_id: str, pid: Optional[int] = None) -> None:
    _ensure_db()
    with _db_connect() as conn:
        if pid is None:
            conn.execute("DELETE FROM waiting_state WHERE session_id = ?", (session_id,))
            logger.debug("[%s] clear_waiting_state by session", session_id)
        else:
            conn.execute(
                "DELETE FROM waiting_state WHERE session_id = ? AND pid = ?",
                (session_id, pid),
            )
            logger.debug("[%s] clear_waiting_state by session+pid: pid=%s", session_id, pid)


def is_waiting_state(session_id: str) -> bool:
    _ensure_db()
    conn = _db_connect()
    try:
        row = conn.execute(
            "SELECT pid FROM waiting_state WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            logger.debug("[%s] is_waiting_state: no row", session_id)
            return False
        pid = row[0]
        if isinstance(pid, int) and _pid_alive(pid):
            logger.debug("[%s] is_waiting_state: true pid=%s", session_id, pid)
            return True
        conn.execute("DELETE FROM waiting_state WHERE session_id = ?", (session_id,))
        logger.info("[%s] is_waiting_state stale row removed: pid=%s", session_id, pid)
        return False
    finally:
        conn.close()


def cleanup_session_state(session_id: str) -> None:
    _ensure_db()
    with _db_connect() as conn:
        conn.execute("DELETE FROM input_queue WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM waiting_state WHERE session_id = ?", (session_id,))


class SessionFiles:
    """管理一个 session 的所有 IPC 文件路径"""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.output_file  = str(IPC_DIR / f"{IPC_PREFIX}out_{session_id}.jsonl")
        self.stderr_file  = str(IPC_DIR / f"{IPC_PREFIX}err_{session_id}.err")
        self.prompt_file  = str(IPC_DIR / f"{IPC_PREFIX}prompt_{session_id}.txt")
        self.marker_file  = str(IPC_DIR / f"{IPC_PREFIX}waiting_{session_id}.marker")
        self._last_wait_call_id: Optional[str] = None
        logger.debug(
            "[%s] session files prepared: output=%s stderr=%s prompt=%s marker=%s backend=sqlite",
            self.session_id,
            self.output_file,
            self.stderr_file,
            self.prompt_file,
            self.marker_file,
        )

    # ---- 状态查询 ----

    @property
    def is_waiting(self) -> bool:
        """检查 Agent 是否准备好接收新输入"""
        if is_waiting_state(self.session_id):
            logger.debug("[%s] is_waiting=true by sqlite waiting_state", self.session_id)
            return True

        if not os.path.exists(self.marker_file):
            logger.debug("[%s] is_waiting=false marker_missing", self.session_id)
            return False

        try:
            with open(self.marker_file, "r", encoding="utf-8") as f:
                marker = json.load(f)
        except (OSError, json.JSONDecodeError):
            # 兼容旧 marker（非 JSON）: 文件存在即视为等待中
            logger.info("[%s] marker not json/readable, fallback waiting=true", self.session_id)
            return True

        marker_session = marker.get("session")
        if marker_session and marker_session != self.session_id:
            _safe_unlink(self.marker_file)
            logger.warning(
                "[%s] marker session mismatch: marker_session=%s marker_file=%s",
                self.session_id,
                marker_session,
                self.marker_file,
            )
            return False

        pid = marker.get("pid")
        if isinstance(pid, int) and pid > 0:
            if _pid_alive(pid):
                logger.debug("[%s] is_waiting=true marker pid alive pid=%s", self.session_id, pid)
                return True
            # marker 对应进程不存在，清理陈旧文件
            _safe_unlink(self.marker_file)
            logger.info("[%s] stale marker removed pid=%s", self.session_id, pid)
            return False

        logger.debug("[%s] is_waiting=true marker exists without pid", self.session_id)
        return True

    # ---- 文件操作 ----

    def write_prompt(self, prompt: str) -> None:
        """写入首轮 prompt 到 prompt 文件"""
        with open(self.prompt_file, "w", encoding="utf-8") as f:
            f.write(prompt)
        logger.debug("[%s] write_prompt ok: bytes=%s file=%s", self.session_id, len(prompt or ""), self.prompt_file)

    def send_input(self, text: str) -> None:
        """写入后续轮指令到 SQLite 输入队列 (wait_input.py 会读取)。"""
        enqueue_input(self.session_id, text)
        logger.info(f"[{self.session_id}] 输入已入队(SQLite): {len(text)} 字节")

    def cleanup(self) -> None:
        """清理所有 IPC 文件"""
        cleanup_session_state(self.session_id)
        logger.debug("[%s] session cleanup begin", self.session_id)
        legacy_input_file = str(IPC_DIR / f"{IPC_PREFIX}input_{self.session_id}.txt")
        legacy_input_tmp = str(IPC_DIR / f"{IPC_PREFIX}input_{self.session_id}.tmp")
        for fpath in [
            self.output_file,
            self.stderr_file,
            self.prompt_file,
            self.marker_file,
            legacy_input_file,
            legacy_input_tmp,
        ]:
            try:
                if os.path.exists(fpath):
                    os.unlink(fpath)
                    logger.debug("[%s] removed ipc file: %s", self.session_id, fpath)
            except OSError:
                logger.warning("[%s] failed removing ipc file: %s", self.session_id, fpath)
                pass

    # ---- 输出读取 (同步生成器, 用于 REPL 线程) ----

    def read_events(self, read_pos: int = 0, timeout: float = 600.0) -> Generator[tuple[dict, int], None, None]:
        """读取 JSONL 输出事件流
        
        从 read_pos 字节位置开始读取 JSONL 输出，解析事件。
        每次循环都重新检查文件是否存在（防止清理期间文件被删除）。

        Yields:
            (event_dict, new_read_pos)

        当检测到 wait_input.py started 或 Agent 进程退出时结束。
        """
        line_buf = ""
        idle = 0
        max_idle = int(timeout / POLL_SLOW)
        poll_interval = POLL_SLOW
        pos = read_pos
        result_success_seen = False
        result_success_at = 0.0

        # 等待输出文件出现
        wait_start = time.time()
        output_file = None
        while not output_file:
            if os.path.exists(self.output_file):
                output_file = self.output_file
                logger.debug("[%s] read_events output file ready: %s", self.session_id, output_file)
                break
            if time.time() - wait_start > 30:
                logger.warning("[%s] read_events exit: output file missing for 30s", self.session_id)
                return
            time.sleep(0.05)

        while True:
            # 每次循环都重新检查文件是否存在（防止清理期间文件被删除）
            if not os.path.exists(output_file):
                idle += 1
                if idle >= max_idle:
                    logger.warning(
                        "[%s] read_events exit: output file disappeared idle=%s max_idle=%s",
                        self.session_id,
                        idle,
                        max_idle,
                    )
                    return
                time.sleep(poll_interval)
                continue

            try:
                with open(output_file, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(pos)
                    new = f.read()
                    if new:
                        logger.debug("[%s] read_events chunk: bytes=%s pos_before=%s", self.session_id, len(new), pos)
                        pos = f.tell()
                        idle = 0
                        poll_interval = POLL_FAST
                        line_buf += new
                        while "\n" in line_buf:
                            line, line_buf = line_buf.split("\n", 1)
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                ev = json.loads(line)
                            except json.JSONDecodeError:
                                logger.warning(
                                    "[%s] read_events json decode failed: line_prefix=%s",
                                    self.session_id,
                                    line[:200],
                                )
                                continue

                            ev_type = ev.get("type", "")
                            ev_sub = ev.get("subtype", "")
                            logger.debug(
                                "[%s] read_events event: type=%s subtype=%s pos=%s",
                                self.session_id,
                                ev_type,
                                ev_sub,
                                pos,
                            )

                            if ev_type == "result" and ev_sub == "success":
                                result_success_seen = True
                                result_success_at = time.time()
                                logger.info(
                                    "[%s] read_events saw result=success, waiting %.1fs for wait_input started",
                                    self.session_id,
                                    WAIT_INPUT_AFTER_RESULT_GRACE_SECS,
                                )

                            # 检测 wait_input.py started = 本轮结束
                            if ev_type == "tool_call" and ev_sub == "started":
                                cmd = _extract_tool_command(ev)
                                if "wait_input.py" in cmd:
                                    call_id = ev.get("call_id")
                                    # 网络重连可能回放同一 call_id 的 started 事件。
                                    # 该重复事件不能再次触发“本轮结束”，否则会提前截断下一轮输出。
                                    if call_id and call_id == self._last_wait_call_id:
                                        logger.debug(
                                            f"[{self.session_id}] 忽略重放 wait_input started 事件: call_id={call_id}"
                                        )
                                        continue
                                    if call_id:
                                        self._last_wait_call_id = call_id
                                    logger.info(
                                        "[%s] wait_input started detected: call_id=%s cmd=%s",
                                        self.session_id,
                                        call_id,
                                        cmd,
                                    )
                                    yield ev, pos
                                    # 等 marker 文件确认就绪
                                    for _ in range(100):
                                        if self.is_waiting:
                                            logger.info("[%s] read_events end: marker confirmed waiting", self.session_id)
                                            return
                                        time.sleep(0.05)
                                    logger.warning(
                                        "[%s] read_events end: wait_input started but marker not ready after 5s",
                                        self.session_id,
                                    )
                                    return

                            yield ev, pos
                    else:
                        idle += 1
                        if idle > 5:
                            poll_interval = POLL_SLOW

                        # 兜底：本轮出现 result=success 后，若长时间没有 wait_input started，
                        # 直接提前退出，避免无意义卡满 600s。
                        if result_success_seen and (time.time() - result_success_at) >= WAIT_INPUT_AFTER_RESULT_GRACE_SECS:
                            logger.warning(
                                "[%s] read_events exit: result=success but no wait_input started after %.1fs",
                                self.session_id,
                                WAIT_INPUT_AFTER_RESULT_GRACE_SECS,
                            )
                            yield {
                                "type": "acli_internal",
                                "subtype": "missing_wait_input_after_result",
                                "grace_secs": WAIT_INPUT_AFTER_RESULT_GRACE_SECS,
                            }, pos
                            return
            except FileNotFoundError:
                idle += 1
                logger.warning("[%s] read_events output file vanished while reading", self.session_id)
            except Exception as e:
                idle += 1
                logger.error("[%s] read_events unexpected error: %s", self.session_id, e)

            if idle >= max_idle:
                logger.warning(
                    "[%s] read_events timeout exit: idle=%s max_idle=%s timeout=%.1fs",
                    self.session_id,
                    idle,
                    max_idle,
                    timeout,
                )
                return

            time.sleep(poll_interval)


def cleanup_all_ipc_files() -> int:
    """清理所有 acli IPC 临时文件，返回清理数量"""
    global _DB_READY
    count = 0
    logger.info("cleanup_all_ipc_files start: dir=%s backend=sqlite", IPC_DIR)
    for pattern in [
        f"{IPC_PREFIX}out_*.jsonl", f"{IPC_PREFIX}err_*.err",
        f"{IPC_PREFIX}prompt_*.txt", f"{IPC_PREFIX}waiting_*.marker",
        f"{IPC_PREFIX}input_*.txt", f"{IPC_PREFIX}input_*.tmp",  # 临时文件
    ]:
        for fpath in glob.glob(str(IPC_DIR / pattern)):
            try:
                os.unlink(fpath)
                count += 1
                logger.debug("cleanup removed file: %s", fpath)
            except OSError:
                logger.warning("cleanup failed removing file: %s", fpath)
                pass

    for fpath in [
        SQLITE_DB_FILE,
        Path(f"{SQLITE_DB_FILE}-wal"),
        Path(f"{SQLITE_DB_FILE}-shm"),
    ]:
        try:
            if fpath.exists():
                fpath.unlink()
                count += 1
                logger.debug("cleanup removed sqlite file: %s", fpath)
        except OSError:
            logger.warning("cleanup failed removing sqlite file: %s", fpath)
            pass
    _DB_READY = False
    logger.info("cleanup_all_ipc_files done: removed=%s", count)
    return count


def _extract_tool_command(ev: dict) -> str:
    """从 tool_call 事件中提取 shell command"""
    tc = ev.get("tool_call", {})
    for k, v in tc.items():
        if isinstance(v, dict):
            cmd = v.get("args", {}).get("command", "")
            if cmd:
                return cmd
    return ""


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _safe_unlink(path: str) -> None:
    try:
        if os.path.exists(path):
            os.unlink(path)
    except OSError:
        pass
