"""
acli Agent 进程管理

负责:
- 启动 Agent CLI 子进程 (--print --stream-json --stream-partial-output)
- 孤儿进程清理
- 进程健康检查
"""
from __future__ import annotations

import logging
import os
import shlex
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional

from config import (
    AGENT_CLI,
    PERSISTENT_PROMPT_TEMPLATE,
    SESSION_IDLE_TIMEOUT_SECS,
    IPC_PREFIX,
    SECOND_REMINDER_ENABLED,
    SECOND_REMINDER_TEMPLATE,
)
from ipc import SessionFiles

logger = logging.getLogger("acli.agent")


class AgentProcess:
    """封装一个 Agent CLI 子进程"""

    def __init__(
        self,
        session_id: str,
        workspace: str,
        model: str,
        mode: Optional[str] = None,
        api_key: Optional[str] = None,
    ):
        self.session_id = session_id
        self.workspace = workspace
        self.model = model
        self.mode = mode
        self.api_key = api_key
        self.files = SessionFiles(session_id)
        self.proc: Optional[subprocess.Popen] = None
        self.pid: int = 0
        self.created_at: float = 0
        self.last_active: float = 0
        self.round_count: int = 0
        self.read_pos: int = 0
        self.process_start_ticks: int = 0
        self.wait_script = str(Path(__file__).parent / "wait_input.py")

    # ---- 生命周期 ----

    def start(self, prompt: str) -> None:
        """启动 Agent CLI 进程, 首轮 prompt"""
        session_id = self.session_id
        logger.info(
            "[%s] 启动 Agent 进程: workspace=%s model=%s mode=%s prompt_bytes=%s",
            session_id,
            self.workspace,
            self.model,
            self.mode,
            len(prompt or ""),
        )
        
        # 清理旧孤儿进程（确保没有竞争）
        logger.info("[%s] 清理孤儿进程...", session_id)
        killed = kill_orphan_agents(session_id)
        if killed > 0:
            logger.info("[%s] 清理了 %s 个孤儿进程", session_id, killed)
            time.sleep(0.5)  # 等待进程完全退出

        # 清理旧 IPC 文件
        logger.info("[%s] 清理旧 IPC 文件...", session_id)
        self.files.cleanup()

        # 构建持久化 prompt
        logger.debug("[%s] wait_script: %s", session_id, self.wait_script)
        first_prompt = self._with_second_reminder(prompt)
        
        full_prompt = PERSISTENT_PROMPT_TEMPLATE.format(
            wait_script=self.wait_script,
            session_id=session_id,
            timeout=SESSION_IDLE_TIMEOUT_SECS,
            workspace=self.workspace,
            user_prompt=first_prompt,
        )

        self.files.write_prompt(full_prompt)
        logger.debug("[%s] Prompt 已写入: %s", session_id, self.files.prompt_file)

        # 构建命令
        args = [
            AGENT_CLI, "-p", "--force", "--trust",
            "--output-format", "stream-json",
            "--stream-partial-output",
            "--model", self.model,
            "--workspace", self.workspace,
        ]
        if self.mode:
            args.extend(["--mode", self.mode])
        if self.api_key:
            args.extend(["--api-key", self.api_key])

        # 使用 shell 方式启动 (因为需要 $(cat ...) )
        shell_cmd = " ".join(_quote(a) for a in args)
        shell_cmd += f' "$(cat {_quote(self.files.prompt_file)})"'
        shell_cmd += f' > {_quote(self.files.output_file)}'
        shell_cmd += f' 2> {_quote(self.files.stderr_file)}'

        env = os.environ.copy()
        if self.api_key:
            env["CURSOR_API_KEY"] = self.api_key

        logger.debug("[%s] 启动 shell 命令: %s", session_id, shell_cmd)
        self.proc = subprocess.Popen(
            shell_cmd,
            shell=True,
            cwd=self.workspace,
            env=env,
            # 完全分离 stdin, 由 wait_input.py 文件通信
            stdin=subprocess.DEVNULL,
            # stdout/stderr 已重定向到文件
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            # 新进程组 (方便清理)
            preexec_fn=os.setsid,
        )
        self.pid = self.proc.pid
        self.created_at = time.time()
        self.last_active = time.time()
        self.round_count = 1
        self.read_pos = 0
        self.process_start_ticks = _read_proc_start_ticks(self.pid) or 0

        logger.info("[%s] Agent 进程已启动: pid=%s model=%s", session_id, self.pid, self.model)
        logger.debug("[%s] IPC 文件路径: output=%s", session_id, self.files.output_file)
        logger.debug(
            "[%s] process identity: start_ticks=%s created_at=%.3f",
            session_id,
            self.process_start_ticks,
            self.created_at,
        )

    def send_input(self, text: str) -> None:
        """发送后续轮指令
        
        重要：每次发送输入后，last_active 时间会更新
        这样空闲计时器会重新开始，确保用户有完整的 24 小时来等待和输入
        """
        prompt_text = self._with_second_reminder(text)
        self.files.send_input(prompt_text)
        self.round_count += 1
        self.last_active = time.time()  # 关键：重新计时，不要删除
        logger.info(
            "第 %s 轮指令已发送: session=%s bytes=%s idle_reset=%.3f",
            self.round_count,
            self.session_id,
            len(prompt_text or ""),
            self.last_active,
        )

    def _with_second_reminder(self, text: str) -> str:
        if not SECOND_REMINDER_ENABLED:
            return text
        body = (text or "").rstrip()
        reminder = SECOND_REMINDER_TEMPLATE.format(
            wait_script=self.wait_script,
            session_id=self.session_id,
            timeout=SESSION_IDLE_TIMEOUT_SECS,
        )
        return f"{body}{reminder}"

    def kill(self) -> None:
        """终止 Agent 进程及其子进程
        
        使用 SIGTERM -> SIGKILL 的两阶段策略，但仅针对特定 PID
        注意：不使用 killpg 以避免意外杀死其他进程组的进程
        """
        if self.proc:
            try:
                # 第一阶段：SIGTERM 允许进程清理资源
                logger.info("[%s] kill phase1 SIGTERM pid=%s", self.session_id, self.proc.pid)
                os.kill(self.proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError) as e:
                logger.warning("[%s] SIGTERM failed pid=%s err=%s", self.session_id, self.proc.pid, e)
            try:
                self.proc.wait(timeout=3)
                logger.info("Agent 进程 %s 已通过 SIGTERM 终止", self.proc.pid)
            except subprocess.TimeoutExpired:
                # 第二阶段：SIGKILL 强制杀死
                try:
                    logger.warning("[%s] kill phase2 SIGKILL pid=%s", self.session_id, self.proc.pid)
                    os.kill(self.proc.pid, signal.SIGKILL)
                    logger.warning("Agent 进程 %s 已通过 SIGKILL 强制终止", self.proc.pid)
                except (ProcessLookupError, PermissionError, OSError) as e:
                    logger.warning("[%s] SIGKILL failed pid=%s err=%s", self.session_id, self.proc.pid, e)
        elif self.pid > 0:
            # 恢复的 session 没有 proc handle，直接用 PID
            try:
                logger.info("[%s] kill restored session by pid: %s", self.session_id, self.pid)
                os.kill(self.pid, signal.SIGTERM)
                time.sleep(0.5)
                try:
                    os.kill(self.pid, 0)  # 检查是否还活着
                    os.kill(self.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass  # 已死亡
            except (ProcessLookupError, PermissionError) as e:
                logger.warning("[%s] kill restored pid failed: pid=%s err=%s", self.session_id, self.pid, e)
        # 也清理对应 session 的孤儿
        kill_orphan_agents(self.session_id)
        self.files.cleanup()
        logger.info("Agent 已终止: session=%s", self.session_id)

    # ---- 状态 ----

    @property
    def is_alive(self) -> bool:
        """检查进程是否仍活着
        
        对于恢复的 session，需要验证 PID 重用情况。
        通过比对 start_time 确保是同一个进程。
        """
        if self.proc is not None:
            alive = self.proc.poll() is None
            if not alive:
                logger.debug("[%s] is_alive=false: proc.poll=%s", self.session_id, self.proc.poll())
            return alive
        # 恢复的 session 没有 proc handle，通过 PID 检查
        if self.pid > 0:
            try:
                os.kill(self.pid, 0)  # 信号 0 只检查权限，不发送信号
                # PID 还活着，需要验证是否是同一个进程（防止 PID 重用）
                # 优先比对 /proc start_ticks
                if self._verify_process_identity():
                    return True
                logger.warning("[%s] pid alive but identity mismatch: pid=%s", self.session_id, self.pid)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        logger.debug("[%s] is_alive=false", self.session_id)
        return False
    
    def _verify_process_identity(self) -> bool:
        """验证 PID 对应的进程是否是当前 Agent
        
        通过比对 start_time 防止 PID 重用导致的假阳性
        """
        if self.pid <= 0:
            return False

        current_ticks = _read_proc_start_ticks(self.pid)
        if current_ticks is None:
            # 非 Linux 或无法读取 /proc：退化为 PID 存活即真
            logger.debug("[%s] process identity fallback: /proc unavailable pid=%s", self.session_id, self.pid)
            return True

        if self.process_start_ticks > 0:
            ok = current_ticks == self.process_start_ticks
            if not ok:
                logger.warning(
                    "[%s] process start_ticks mismatch: expected=%s actual=%s pid=%s",
                    self.session_id,
                    self.process_start_ticks,
                    current_ticks,
                    self.pid,
                )
            return ok

        # 兼容旧元数据（没有 process_start_ticks）
        # 用 created_at + btime 近似估算启动 ticks。
        expected_ticks = _estimate_start_ticks_from_created_at(self.created_at)
        if expected_ticks is None:
            logger.debug("[%s] process identity fallback: expected_ticks unavailable", self.session_id)
            return True

        clk = _clock_ticks_per_second()
        ok = abs(current_ticks - expected_ticks) <= clk * 3
        if not ok:
            logger.warning(
                "[%s] process start_ticks tolerance exceeded: expected~=%s actual=%s clk=%s",
                self.session_id,
                expected_ticks,
                current_ticks,
                clk,
            )
        return ok

    @property
    def is_waiting(self) -> bool:
        return self.files.is_waiting

    @property
    def idle_seconds(self) -> float:
        return time.time() - self.last_active

    def wait_for_ready(self, timeout: float = 30.0) -> bool:
        """等待 Agent 进入 wait_input.py 等待状态"""
        start = time.time()
        checks = 0
        while time.time() - start < timeout:
            checks += 1
            if self.files.is_waiting:
                logger.info("[%s] wait_for_ready ok: checks=%s elapsed=%.2fs", self.session_id, checks, time.time() - start)
                return True
            if not self.is_alive:
                logger.error("[%s] wait_for_ready failed: process dead checks=%s", self.session_id, checks)
                return False
            if checks % 20 == 0:
                logger.debug(
                    "[%s] wait_for_ready pending: checks=%s elapsed=%.2fs marker=%s",
                    self.session_id,
                    checks,
                    time.time() - start,
                    os.path.exists(self.files.marker_file),
                )
            time.sleep(0.05)
        logger.error("[%s] wait_for_ready timeout: checks=%s timeout=%.1fs", self.session_id, checks, timeout)
        return False


# ========== 孤儿进程清理 ==========


def kill_orphan_agents(session_id: str = "") -> int:
    """杀死孤儿 Agent 进程, 返回清理数量
    
    Args:
        session_id: 指定 session 只清理该 session, 为空清理所有 acli 进程
    """
    killed = 0
    my_pid = os.getpid()
    wait_script_path = str(Path(__file__).parent / "wait_input.py")

    patterns = []
    if session_id:
        patterns.append(f"{IPC_PREFIX}prompt_{session_id}")
        patterns.append(f"{wait_script_path} {session_id}")
    else:
        patterns.append(f"{IPC_PREFIX}prompt_")
        patterns.append(f"{wait_script_path} {IPC_PREFIX[:-1]}")

    for pattern in patterns:
        logger.debug("orphan scan pattern: %s", pattern)
        try:
            result = subprocess.run(
                ["pgrep", "-f", "--exact" if session_id else "-f", pattern]
                if not session_id else ["pgrep", "-f", pattern],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    if not line.strip():
                        continue
                    pid = int(line.strip())
                    if pid == my_pid:
                        continue
                    # Verify the matched process is really ours by checking /proc cmdline
                    if not _verify_orphan_process(pid, pattern):
                        logger.debug("orphan skip non-matching pid=%s pattern=%s", pid, pattern)
                        continue
                    try:
                        logger.info("orphan kill SIGTERM: pid=%s pattern=%s", pid, pattern)
                        os.kill(pid, signal.SIGTERM)
                        killed += 1
                    except (ProcessLookupError, PermissionError) as e:
                        logger.warning("orphan kill failed: pid=%s pattern=%s err=%s", pid, pattern, e)
            else:
                logger.debug("orphan scan no match: pattern=%s rc=%s", pattern, result.returncode)
        except (subprocess.TimeoutExpired, ValueError, Exception) as e:
            logger.warning("orphan scan failed: pattern=%s err=%s", pattern, e)
            continue

    if killed > 0:
        logger.info("清理孤儿进程: session=%s killed=%s", session_id or "all", killed)
        time.sleep(0.3)

    return killed


def _verify_orphan_process(pid: int, pattern: str) -> bool:
    """Double-check via /proc that the PID is an acli-related process."""
    try:
        with open(f"/proc/{pid}/cmdline", "r") as f:
            cmdline = f.read().replace("\0", " ")
        return "wait_input.py" in cmdline or "acli_prompt_" in cmdline or "agent" in cmdline
    except (OSError, PermissionError):
        return False


def _quote(s: str) -> str:
    """Shell-safe quoting using shlex to prevent injection."""
    return shlex.quote(s)


def _read_proc_start_ticks(pid: int) -> Optional[int]:
    """读取 /proc/<pid>/stat 的 starttime ticks（Linux）。"""
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as f:
            stat_fields = f.read().split()
        if len(stat_fields) < 22:
            return None
        return int(stat_fields[21])
    except (OSError, ValueError, IndexError):
        return None


def _clock_ticks_per_second() -> int:
    try:
        return int(os.sysconf("SC_CLK_TCK"))
    except (ValueError, OSError, AttributeError):
        return 100


def _read_boot_time_epoch() -> Optional[int]:
    """读取系统启动 epoch 秒（/proc/stat btime）。"""
    try:
        with open("/proc/stat", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("btime "):
                    return int(line.split()[1])
    except (OSError, ValueError):
        return None
    return None


def _estimate_start_ticks_from_created_at(created_at_epoch: float) -> Optional[int]:
    if created_at_epoch <= 0:
        return None
    btime = _read_boot_time_epoch()
    if btime is None:
        return None
    since_boot = created_at_epoch - btime
    if since_boot < 0:
        return None
    return int(since_boot * _clock_ticks_per_second())
