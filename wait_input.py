#!/usr/bin/env python3
"""
Agent 持久会话等待脚本
被 Agent 的 Shell 工具调用, 阻塞等待下一轮用户输入

用法: python3 wait_input.py <session_id> [timeout_seconds]

文件约定:
  {IPC_DIR}/{IPC_PREFIX}waiting_{session_id}.marker    等待就绪标记
  {IPC_DIR}/{IPC_PREFIX}ipc.sqlite3                    输入队列与 waiting_state
"""
import json
import logging
import os
import signal
import sys
import time

from config import IPC_RUNTIME_DIR, IPC_PREFIX, POLL_INPUT
from ipc import dequeue_input, set_waiting_state, clear_waiting_state
from logging_setup import configure_logging

configure_logging(
    component="wait_input",
    default_filename="acli.wait_input.log",
    file_env_var="ACLI_WAIT_INPUT_LOG_FILE",
    enable_console=True,
)
logger = logging.getLogger("acli.wait_input")

session = sys.argv[1] if len(sys.argv) > 1 else "default"
timeout = int(sys.argv[2]) if len(sys.argv) > 2 else 86400  # 24 小时，最长支持后台时间
trace_every = max(1, int(os.environ.get("ACLI_WAIT_INPUT_TRACE_EVERY", "1")))

ipc_dir = IPC_RUNTIME_DIR
ipc_dir.mkdir(parents=True, exist_ok=True)

marker_file = ipc_dir / f"{IPC_PREFIX}waiting_{session}.marker"

logger.info("等待脚本启动: session=%s timeout=%ss", session, timeout)
logger.info("IPC 目录: %s", ipc_dir)
logger.info("标记文件: %s", marker_file)
logger.info(
    "运行上下文: pid=%s ppid=%s cwd=%s backend=sqlite poll=%.3fs trace_every=%s",
    os.getpid(),
    os.getppid(),
    os.getcwd(),
    POLL_INPUT,
    trace_every,
)


def _cleanup_waiting(reason: str) -> None:
    """清理 waiting 标记/状态，确保退出前状态一致。"""
    logger.info("开始清理等待状态: reason=%s", reason)
    try:
        clear_waiting_state(session, os.getpid())
        logger.debug("已清理 waiting_state: session=%s pid=%s", session, os.getpid())
    except Exception as e:
        logger.warning("清理 waiting_state 失败: %s", e)

    try:
        if marker_file.exists():
            marker_file.unlink()
            logger.debug("已删除标记文件: %s", marker_file)
    except OSError as e:
        logger.warning("删除标记文件失败: %s", e)


_shutdown_requested: int = 0


def _handle_signal(signum: int, _frame) -> None:
    global _shutdown_requested
    _shutdown_requested = signum


for _sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    try:
        signal.signal(_sig, _handle_signal)
    except Exception:
        pass

# 标记等待状态 (关键：这个文件的出现 = Agent 进入等待状态)
try:
    fd = os.open(str(marker_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({
            "status": "waiting",
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "session": session,
            "pid": os.getpid(),
        }, f)
    logger.info("标记文件已创建: %s", marker_file)
except OSError as e:
    logger.error("创建标记文件失败: %s", e)
    print("[MARKER_CREATE_FAILED]")
    sys.exit(1)

try:
    set_waiting_state(session, os.getpid())
except Exception as e:
    logger.error("写入 waiting_state 失败: %s", e)
    print("[WAITING_STATE_FAILED]")
    sys.exit(1)

# 进入等待循环
waited = 0.0
poll_interval = POLL_INPUT
check_count = 0

while waited < timeout:
    if _shutdown_requested:
        sig_name = signal.Signals(_shutdown_requested).name
        logger.warning("处理延迟信号退出: signal=%s(%s)", _shutdown_requested, sig_name)
        _cleanup_waiting(f"signal:{sig_name}")
        sys.exit(128 + _shutdown_requested)

    check_count += 1
    if check_count % trace_every == 0:
        logger.debug(
            "轮询状态: check=%s waited=%.2fs timeout=%ss backend=sqlite marker_exists=%s",
            check_count,
            waited,
            timeout,
            marker_file.exists(),
        )

    try:
        content = dequeue_input(session)
    except Exception as e:
        logger.warning("读取 SQLite 队列失败: %s", e)
        content = None

    if content is not None:
        logger.info("从 SQLite 队列读取输入: %s 字节 (第 %s 次检查)", len(content), check_count)
        _cleanup_waiting("sqlite_input_ready")

        print(content, end='')
        logger.info("已向 Agent 输出内容，退出: reason=sqlite_input_ready")
        sys.exit(0)
    
    time.sleep(poll_interval)
    waited += poll_interval
    
    if check_count % 20 == 0:
        logger.debug("等待中... %ss / %ss", int(waited), timeout)

# 超时
logger.error("等待超时: %ss 内未收到输入", timeout)
_cleanup_waiting("timeout")

print("[SESSION_TIMEOUT]")
logger.info("输出超时标记，退出")
sys.exit(0)
