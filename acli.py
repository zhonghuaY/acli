#!/usr/bin/env python3
"""
acli — Agent CLI REPL

轻量级命令行工具, 复用 Cursor Agent CLI 的持久会话机制,
为每个工作目录维护独立的 Agent 实例。

用法:
  acli                          在当前目录启动交互式 REPL
  acli -p "写一个 hello world"   单次执行模式
  acli --model opus-4.6         指定模型
  acli --workspace /path/to/dir 指定工作目录
  acli status                   查看活跃会话
  acli kill                     杀掉当前目录的 Agent
  acli cleanup                  清理所有 Agent 和临时文件
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

# 确保 acli 目录在 path 中 (方便直接 python3 acli.py 运行)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    DEFAULT_MODEL,
    AGENT_CLI,
    Color,
    POPULAR_MODELS,
    resolve_model,
    MODEL_ALIASES,
    SESSION_IDLE_TIMEOUT_SECS,
    IPC_BACKEND,
    IPC_RUNTIME_DIR,
    IPC_BACKEND_FORCED,
    IPC_BACKEND_REQUESTED,
)
from session_manager import SessionManager
from agent_process import kill_orphan_agents
from ipc import cleanup_all_ipc_files
from repl import run_repl
from logging_setup import configure_logging

logger = logging.getLogger("acli.main")


def main():
    # 构建模型帮助文本
    model_lines = "\n".join(f"    {mid:<24} {desc}" for mid, desc in POPULAR_MODELS)
    alias_examples = "opus-thinking, sonnet, codex, gpt, gemini, flash, auto ..."

    p = argparse.ArgumentParser(
        prog="acli",
        description="Agent CLI REPL — 轻量级持久化 Agent 交互工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
常用模型 (--model / -m):
{model_lines}

模型别名 (自动解析):
    {alias_examples}
    例: acli -m opus-thinking  →  opus-4.6-thinking

示例:
  acli                              交互式 REPL (默认 {DEFAULT_MODEL})
  acli -m opus-4.6-thinking         使用 Opus Thinking 模型
  acli -m opus-thinking             同上 (别名)
  acli -m sonnet-4.5-thinking       使用 Sonnet Thinking 模型
  acli -p "解释这个项目的架构"        单次执行
  acli --mode plan                  规划模式 (只读分析)
  acli models                       列出所有可用模型
  acli status                       查看活跃会话
  acli kill                         杀掉当前目录的 Agent
  acli cleanup                      清理所有 Agent
""",
    )

    # 子命令
    sub = p.add_subparsers(dest="command", help="管理命令")

    sub_status = sub.add_parser("status", help="查看所有活跃会话")

    sub_kill = sub.add_parser("kill", help="杀掉当前目录的 Agent")
    sub_kill.add_argument("--all", action="store_true", help="杀掉所有 Agent")

    sub_cleanup = sub.add_parser("cleanup", help="清理所有 Agent 和临时文件")

    sub_models = sub.add_parser("models", help="列出可用模型")

    # 主参数
    p.add_argument("-p", "--print", dest="one_shot", metavar="PROMPT",
                   help="单次执行模式, 执行后退出")
    p.add_argument("--model", "-m", default=DEFAULT_MODEL,
                   help=f"使用的模型 (默认: {DEFAULT_MODEL})")
    p.add_argument("--mode", choices=["plan", "ask"],
                   help="Agent 模式: plan=只读分析, ask=问答")
    p.add_argument("--workspace", "-w", default=os.getcwd(),
                   help="工作目录 (默认: 当前目录)")
    p.add_argument("--api-key", help="Cursor API Key")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="详细日志")

    args = p.parse_args()

    log_file = configure_logging(
        component="acli",
        default_filename="acli.log",
        file_env_var="ACLI_LOG_FILE",
        enable_console=True,
    )

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    logger.info(
        "acli start: argv=%s command=%s workspace=%s model=%s mode=%s log_file=%s",
        sys.argv,
        args.command,
        args.workspace,
        args.model,
        args.mode,
        log_file,
    )
    logger.info(
        "runtime config: agent_cli=%s idle_timeout_secs=%s ipc_backend=%s ipc_dir=%s",
        AGENT_CLI,
        SESSION_IDLE_TIMEOUT_SECS,
        IPC_BACKEND,
        IPC_RUNTIME_DIR,
    )
    if IPC_BACKEND_FORCED:
        logger.warning(
            "ACLI_IPC_BACKEND=%s is no longer supported; forcing sqlite backend",
            IPC_BACKEND_REQUESTED,
        )

    c = Color

    # ---- 子命令 ----
    if args.command == "status":
        logger.info("subcommand status start")
        mgr = SessionManager()
        sessions = mgr.list_sessions()
        logger.info("subcommand status result: sessions=%s", len(sessions))
        if not sessions:
            print(f"{c.DIM}没有活跃的会话{c.RESET}")
        else:
            print(f"\n{c.BOLD}活跃会话:{c.RESET}")
            for s in sessions:
                marker = f"{c.GREEN}●{c.RESET}" if s["alive"] else f"{c.RED}●{c.RESET}"
                state = "已退出" if not s["alive"] else ("等待中" if s["waiting"] else "运行中")
                wd = s["workspace"]
                print(f"  {marker} {wd}")
                print(f"    Model: {s['model']} | State: {state} "
                      f"| Rounds: {s['rounds']} | Idle: {s['idle_min']}min | PID: {s['pid']}")
            print()
        return

    if args.command == "kill":
        logger.info("subcommand kill start: all=%s workspace=%s model=%s", args.all, args.workspace, args.model)
        mgr = SessionManager()
        if args.all:
            mgr.stop_and_kill_all()
            kill_orphan_agents()
            n = cleanup_all_ipc_files()
            logger.info("subcommand kill --all done: cleaned_files=%s", n)
            print(f"{c.GREEN}所有 Agent 已终止, 清理 {n} 个临时文件{c.RESET}")
        else:
            workspace = os.path.abspath(args.workspace)
            resolved_model, hint = resolve_model(args.model)
            if hint:
                print(f"{c.CYAN}{hint}{c.RESET}")
                logger.info("model hint during kill: %s", hint)
            if mgr.kill_session(workspace, resolved_model):
                logger.info("subcommand kill done: workspace=%s model=%s result=found", workspace, resolved_model)
                print(f"{c.GREEN}Agent 已终止: {workspace} ({resolved_model}){c.RESET}")
            else:
                logger.info("subcommand kill done: workspace=%s model=%s result=not_found", workspace, resolved_model)
                print(f"{c.YELLOW}未找到匹配的会话{c.RESET}")
        return

    if args.command == "cleanup":
        logger.info("subcommand cleanup start")
        kill_orphan_agents()
        n = cleanup_all_ipc_files()
        mgr = SessionManager()
        mgr.stop_and_kill_all()
        logger.info("subcommand cleanup done: cleaned_files=%s", n)
        print(f"{c.GREEN}清理完成: 清理 {n} 个临时文件, 所有 Agent 已终止{c.RESET}")
        return

    if args.command == "models":
        import subprocess as sp
        try:
            logger.info("subcommand models start")
            result = sp.run([AGENT_CLI, "--list-models"], capture_output=True, text=True, timeout=10)
            logger.info("subcommand models done: returncode=%s stdout_bytes=%s stderr_bytes=%s", result.returncode, len(result.stdout or ""), len(result.stderr or ""))
            print(result.stdout)
        except Exception as e:
            logger.exception("subcommand models failed")
            print(f"{c.RED}获取模型列表失败: {e}{c.RESET}")
        return

    # ---- REPL / 单次执行 ----
    workspace = os.path.abspath(args.workspace)

    # 检查 Agent CLI 是否存在
    if not os.path.isfile(AGENT_CLI):
        logger.error("agent cli not found: %s", AGENT_CLI)
        print(f"{c.RED}Agent CLI 未找到: {AGENT_CLI}{c.RESET}")
        print(f"{c.DIM}请设置 ACLI_AGENT_CLI 环境变量或安装 Cursor Agent CLI{c.RESET}")
        sys.exit(1)

    # 解析模型别名/模糊匹配
    resolved_model, hint = resolve_model(args.model)
    if hint:
        print(f"{c.CYAN}{hint}{c.RESET}")
        logger.info("model hint: %s", hint)

    mgr = SessionManager()
    mgr.start_cleanup_thread()

    try:
        run_repl(
            mgr=mgr,
            workspace=workspace,
            model=resolved_model,
            mode=args.mode,
            api_key=args.api_key,
            one_shot=args.one_shot,
        )
    except KeyboardInterrupt:
        print(f"\n{c.DIM}Bye!{c.RESET}")
    finally:
        mgr.stop()  # 只停清理线程, 不杀 Agent


if __name__ == "__main__":
    main()
