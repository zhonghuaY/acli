"""
acli 会话管理器

核心策略:
- session_id = hash(abs_workspace + model)
- 每个 (目录, 模型) 组合独立一个 Agent CLI 进程
- 空闲 10 小时自动清理
- 持久化 session 元数据到 ~/.acli/sessions/
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, Optional

from config import (
    SESSION_DB_DIR,
    SESSION_IDLE_TIMEOUT_SECS,
)
from agent_process import AgentProcess

logger = logging.getLogger("acli.session")


def make_session_id(workspace: str, model: str) -> str:
    """生成稳定的 session ID"""
    raw = f"{os.path.abspath(workspace)}:{model}"
    return f"acli_{hashlib.md5(raw.encode()).hexdigest()[:12]}"


class SessionManager:
    """管理所有活跃的 Agent 会话"""

    def __init__(self):
        self._sessions: Dict[str, AgentProcess] = {}
        self._lock = threading.Lock()
        self._cleanup_thread: Optional[threading.Thread] = None
        self._running = False
        self._stop_event = threading.Event()

        # 确保 session DB 目录存在
        SESSION_DB_DIR.mkdir(parents=True, exist_ok=True)

        # 启动时恢复已知 session
        self._restore_sessions()
        logger.info("SessionManager initialized: restored=%s", len(self._sessions))

    # ---- 对外接口 ----

    def get_or_create(
        self,
        workspace: str,
        model: str,
        prompt: str,
        mode: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> AgentProcess:
        """获取或创建 Agent 会话

        如果已有存活会话 → 直接发送指令（SQLite 队列保证顺序）
        否则 → 启动新会话
        """
        sid = make_session_id(workspace, model)
        logger.debug(
            "get_or_create called: sid=%s workspace=%s model=%s prompt_len=%s mode=%s",
            sid,
            workspace,
            model,
            len(prompt or ""),
            mode,
        )

        with self._lock:
            agent = self._sessions.get(sid)
            logger.debug(
                "[%s] session lookup: found=%s total_sessions=%s",
                sid,
                bool(agent),
                len(self._sessions),
            )

            if agent and agent.is_alive:
                logger.debug(
                    "[%s] existing alive session: waiting=%s rounds=%s idle=%.2fs",
                    sid,
                    agent.is_waiting,
                    agent.round_count,
                    agent.idle_seconds,
                )
                if agent.is_waiting:
                    agent.send_input(prompt)
                    self._save_session_meta(agent)
                    return agent

                # Agent 忙碌：先推进 read_pos 到文件末尾，跳过上轮残留输出，
                # 避免 render_agent_output 显示上一轮的尾巴。
                try:
                    if os.path.exists(agent.files.output_file):
                        old_pos = agent.read_pos
                        agent.read_pos = os.path.getsize(agent.files.output_file)
                        logger.info(
                            "[%s] busy: advanced read_pos %s -> %s to skip stale output",
                            sid, old_pos, agent.read_pos,
                        )
                except OSError as e:
                    logger.warning("[%s] busy: failed to advance read_pos: %s", sid, e)
                agent.send_input(prompt)
                self._save_session_meta(agent)
                return agent

            elif agent:
                # 进程已死, 需要重启
                logger.info("[%s] Agent 进程已退出, 重启", sid)
                agent.files.cleanup()

            # 创建新会话 (在锁内创建并注册，防止并发双创建)
            logger.info("[%s] create new AgentProcess", sid)
            agent = AgentProcess(
                session_id=sid,
                workspace=os.path.abspath(workspace),
                model=model,
                mode=mode,
                api_key=api_key,
            )
            self._sessions[sid] = agent

        # start() 可能耗时（清理孤儿、写 prompt），在锁外执行
        agent.start(prompt)
        self._save_session_meta(agent)
        logger.info(
            "[%s] new session ready: pid=%s workspace=%s model=%s",
            sid,
            agent.pid,
            agent.workspace,
            agent.model,
        )
        return agent

    def get_session(self, workspace: str, model: str) -> Optional[AgentProcess]:
        """查找已有会话 (不创建)"""
        sid = make_session_id(workspace, model)
        agent = self._sessions.get(sid)
        logger.debug(
            "get_session: sid=%s workspace=%s model=%s found=%s",
            sid,
            workspace,
            model,
            bool(agent),
        )
        return agent

    def kill_session(self, workspace: str, model: str) -> bool:
        """杀掉指定会话"""
        sid = make_session_id(workspace, model)
        logger.info("kill_session requested: sid=%s workspace=%s model=%s", sid, workspace, model)
        with self._lock:
            agent = self._sessions.pop(sid, None)
        if agent:
            agent.kill()
            self._remove_session_meta(sid)
            logger.info("kill_session done: sid=%s", sid)
            return True
        logger.info("kill_session no-op (not found): sid=%s", sid)
        return False

    def new_session(self, workspace: str, model: str) -> bool:
        """清除上下文, 重新开始 (杀掉旧 Agent, 下次 get_or_create 会创建新的)"""
        logger.info("new_session requested: workspace=%s model=%s", workspace, model)
        return self.kill_session(workspace, model)

    def list_sessions(self) -> list[dict]:
        """列出所有活跃会话"""
        result = []
        to_remove = []
        with self._lock:
            for sid, agent in self._sessions.items():
                alive = agent.is_alive
                if not alive:
                    to_remove.append(sid)
                    continue
                result.append({
                    "session_id": sid,
                    "workspace": agent.workspace,
                    "model": agent.model,
                    "pid": agent.pid,
                    "alive": alive,
                    "waiting": agent.is_waiting,
                    "rounds": agent.round_count,
                    "idle_min": round(agent.idle_seconds / 60, 1),
                })
            for sid in to_remove:
                self._sessions.pop(sid, None)
                self._remove_session_meta(sid)
                logger.info("[%s] removed from list_sessions due to dead process", sid)
        logger.debug("list_sessions done: active=%s removed=%s", len(result), len(to_remove))
        return result

    # ---- 清理线程 ----

    def start_cleanup_thread(self) -> None:
        """启动后台清理线程"""
        if self._cleanup_thread and self._cleanup_thread.is_alive():
            logger.debug("cleanup thread already running")
            return
        self._running = True
        self._cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        self._cleanup_thread.start()
        logger.info(
            "cleanup thread started: thread=%s interval=300s idle_timeout=%.1fh",
            self._cleanup_thread.name,
            SESSION_IDLE_TIMEOUT_SECS / 3600,
        )

    def stop(self) -> None:
        """停止所有"""
        self._running = False
        self._stop_event.set()
        logger.info("SessionManager stop called: cleanup thread stop requested")

    def stop_and_kill_all(self) -> None:
        """停止并杀死所有 Agent"""
        self._running = False
        logger.info("stop_and_kill_all begin")
        with self._lock:
            for sid, agent in list(self._sessions.items()):
                logger.info("[%s] stop_and_kill_all killing agent", sid)
                agent.kill()
                self._remove_session_meta(sid)
            self._sessions.clear()
        # 清理遗留元数据，保证 kill --all 后状态一致
        try:
            for meta_file in SESSION_DB_DIR.glob("*.json"):
                meta_file.unlink(missing_ok=True)
        except OSError:
            pass
        logger.info("stop_and_kill_all done")

    def _cleanup_loop(self) -> None:
        """定期检查并清理空闲超时的 session"""
        while self._running:
            if self._stop_event.wait(timeout=300):
                break
            logger.debug("cleanup loop tick: begin idle-session scan")
            self._cleanup_idle_sessions()

    def _cleanup_idle_sessions(self) -> None:
        """清理空闲超时的会话"""
        to_remove = []
        with self._lock:
            for sid, agent in self._sessions.items():
                logger.debug(
                    "[%s] cleanup scan: alive=%s waiting=%s idle=%.2fs rounds=%s",
                    sid,
                    agent.is_alive,
                    agent.is_waiting,
                    agent.idle_seconds,
                    agent.round_count,
                )
                if not agent.is_alive:
                    to_remove.append(sid)
                elif agent.idle_seconds > SESSION_IDLE_TIMEOUT_SECS:
                    logger.info("[%s] 空闲超时 (%.1fh), 清理", sid, agent.idle_seconds / 3600)
                    agent.kill()
                    to_remove.append(sid)
            for sid in to_remove:
                self._sessions.pop(sid, None)
                self._remove_session_meta(sid)
        if to_remove:
            logger.info("cleanup removed sessions: count=%s ids=%s", len(to_remove), ",".join(to_remove))
        else:
            logger.debug("cleanup removed sessions: count=0")

    # ---- 持久化元数据 ----

    def _save_session_meta(self, agent: AgentProcess) -> None:
        """保存 session 元数据 (用于恢复)

        Uses atomic write-to-temp + rename to prevent corruption
        if the process is killed mid-write.
        """
        meta = {
            "session_id": agent.session_id,
            "workspace": agent.workspace,
            "model": agent.model,
            "pid": agent.pid,
            "process_start_ticks": agent.process_start_ticks,
            "created_at": agent.created_at,
            "last_active": agent.last_active,
            "round_count": agent.round_count,
        }
        meta_file = SESSION_DB_DIR / f"{agent.session_id}.json"
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(SESSION_DB_DIR), suffix=".tmp", prefix=".meta_"
            )
            with os.fdopen(fd, "w") as f:
                json.dump(meta, f, indent=2)
            os.replace(tmp_path, str(meta_file))
            logger.debug("[%s] session meta saved: %s", agent.session_id, meta_file)
        except Exception as e:
            logger.warning("保存 session 元数据失败: %s", e)
            try:
                os.unlink(tmp_path)
            except (OSError, NameError):
                pass

    def _remove_session_meta(self, session_id: str) -> None:
        meta_file = SESSION_DB_DIR / f"{session_id}.json"
        try:
            if meta_file.exists():
                meta_file.unlink()
                logger.debug("[%s] session meta removed: %s", session_id, meta_file)
        except OSError:
            logger.warning("[%s] session meta remove failed: %s", session_id, meta_file)
            pass

    def _restore_sessions(self) -> None:
        """从磁盘恢复已知 session (检查进程是否还活着)"""
        if not SESSION_DB_DIR.exists():
            return

        restored = 0
        for meta_file in SESSION_DB_DIR.glob("*.json"):
            try:
                logger.debug("restoring session from meta: %s", meta_file)
                with open(meta_file) as f:
                    meta = json.load(f)

                sid = meta["session_id"]
                pid = meta.get("pid", 0)

                # 恢复 AgentProcess 对象 (仅元数据, 无法恢复 proc handle)
                agent = AgentProcess(
                    session_id=sid,
                    workspace=meta["workspace"],
                    model=meta["model"],
                )
                agent.pid = pid
                agent.process_start_ticks = int(meta.get("process_start_ticks", 0) or 0)
                agent.created_at = meta.get("created_at", time.time())
                agent.last_active = meta.get("last_active", time.time())
                agent.round_count = meta.get("round_count", 0)

                # 恢复后立即做一次存活校验，避免挂入错误 PID
                if not agent.is_alive:
                    logger.info("[%s] restore skipped: pid not alive (%s)", sid, pid)
                    meta_file.unlink(missing_ok=True)
                    continue

                # 恢复时跳到文件末尾，避免重放旧轮次输出
                try:
                    if os.path.exists(agent.files.output_file):
                        agent.read_pos = os.path.getsize(agent.files.output_file)
                        logger.debug("[%s] restore read_pos from output size: %s", sid, agent.read_pos)
                except OSError as e:
                    logger.warning("[%s] restore read_pos failed: %s", sid, e)

                self._sessions[sid] = agent
                restored += 1
                logger.info("[%s] restore success: pid=%s workspace=%s model=%s", sid, pid, agent.workspace, agent.model)

            except (json.JSONDecodeError, KeyError, Exception) as e:
                logger.warning("恢复 session 失败 (%s): %s", meta_file.name, e)
                try:
                    meta_file.unlink()
                except OSError:
                    pass

        if restored > 0:
            logger.info("恢复了 %s 个 session", restored)
