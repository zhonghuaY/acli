"""
acli logging setup utilities.

目标：
- 统一将日志落到文件（支持滚动）
- 在功能稳定前默认使用 DEBUG 级别，最大化可观测性
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional


def _parse_level(raw: str) -> int:
    value = (raw or "DEBUG").strip().upper()
    return getattr(logging, value, logging.DEBUG)


def configure_logging(
    *,
    component: str,
    default_filename: str,
    file_env_var: str,
    enable_console: bool = True,
    console_level: Optional[int] = None,
) -> Path:
    """
    配置 root logger 到文件 + 控制台。

    Args:
        component: 组件名（用于首条日志）
        default_filename: 默认日志文件名（放在 ACLI_LOG_DIR 下）
        file_env_var: 允许覆盖日志文件路径的环境变量名
        enable_console: 是否同时输出到 stderr
    """
    log_dir = Path(
        os.environ.get("ACLI_LOG_DIR", str(Path.home() / ".local" / "log"))
    )
    log_dir.mkdir(parents=True, exist_ok=True)

    log_file = Path(os.environ.get(file_env_var, str(log_dir / default_filename)))
    log_file.parent.mkdir(parents=True, exist_ok=True)

    level = _parse_level(os.environ.get("ACLI_LOG_LEVEL", "DEBUG"))
    resolved_console_level = (
        console_level
        if console_level is not None
        else _parse_level(os.environ.get("ACLI_CONSOLE_LOG_LEVEL", "ERROR"))
    )
    max_bytes = int(os.environ.get("ACLI_LOG_MAX_BYTES", str(100 * 1024 * 1024)))
    backup_count = int(os.environ.get("ACLI_LOG_BACKUP_COUNT", "30"))

    fmt = (
        "%(asctime)s [%(levelname)s] %(name)s "
        "pid=%(process)d tid=%(thread)d %(filename)s:%(lineno)d - %(message)s"
    )
    formatter = logging.Formatter(fmt)

    root = logging.getLogger()
    root.setLevel(level)

    # 清理旧 handler，避免重复输出/重复写文件。
    for h in list(root.handlers):
        root.removeHandler(h)

    file_handler = RotatingFileHandler(
        str(log_file),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    if enable_console:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setLevel(resolved_console_level)
        stream_handler.setFormatter(formatter)
        root.addHandler(stream_handler)

    logging.getLogger("acli.logging").info(
        "logging configured: component=%s file=%s level=%s console_level=%s max_bytes=%s backups=%s",
        component,
        log_file,
        logging.getLevelName(level),
        logging.getLevelName(resolved_console_level),
        max_bytes,
        backup_count,
    )
    return log_file
