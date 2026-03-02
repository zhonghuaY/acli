"""
acli REPL — 交互式终端

功能:
- readline 补全 (命令 + /skill 模糊匹配)
- 实时流式输出 Agent 回复
- 内置命令 (/help, /status, /new, /model, /mode, /models, /quit, /kill)
- /skill 或 / + 名称: 模糊匹配 skill 目录
- 多行输入 (\\续行)
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import readline
import select
import subprocess
import sys
import termios
import threading
import time
from pathlib import Path
from typing import Optional

from config import Color, AGENT_CLI, DEFAULT_MODEL, SKILL_DIRS, MODEL_ALIASES, resolve_model
from session_manager import SessionManager, make_session_id
from agent_process import AgentProcess
from ipc import extract_tool_command

logger = logging.getLogger("acli.repl")

# ========== Skill 索引 ==========

_skill_cache: Optional[list[dict]] = None


def _load_skills() -> list[dict]:
    """扫描 SKILL_DIRS 下所有 skill 目录"""
    global _skill_cache
    if _skill_cache is not None:
        return _skill_cache

    skills = []
    logger.debug("loading skills from dirs: %s", [str(p) for p in SKILL_DIRS])
    for skill_dir in SKILL_DIRS:
        if not skill_dir.exists():
            logger.debug("skill dir not found: %s", skill_dir)
            continue
        for entry in sorted(skill_dir.iterdir()):
            if entry.is_dir():
                skill_md = entry / "SKILL.md"
                if skill_md.exists():
                    skills.append({
                        "name": entry.name,
                        "path": str(skill_md),
                        "dir": str(entry),
                    })
    _skill_cache = skills
    logger.info("skills loaded: count=%s", len(skills))
    return skills


def _fuzzy_match_skills(query: str) -> list[dict]:
    """模糊匹配 skill 名称

    匹配规则:
    1. 前缀匹配 (优先)
    2. 子串匹配
    3. 每个字符依序匹配 (fuzzy)
    """
    skills = _load_skills()
    q = query.lower()
    if not q:
        return skills

    # 前缀
    prefix = [s for s in skills if s["name"].lower().startswith(q)]
    if prefix:
        return prefix

    # 子串
    substr = [s for s in skills if q in s["name"].lower()]
    if substr:
        return substr

    # Fuzzy: 每个字符依序出现
    def fuzzy(name: str, pattern: str) -> bool:
        it = iter(name.lower())
        return all(c in it for c in pattern.lower())

    return [s for s in skills if fuzzy(s["name"], q)]


# ========== Readline 补全 ==========

# ========== 多行粘贴 (Expert-level) ==========
#
# 技术方案:
# 1. Bracketed Paste Mode — 现代终端标准 (xterm, iTerm, gnome-terminal, kitty...)
#    - 终端在粘贴时包裹: \033[200~ <粘贴内容> \033[201~
#    - 即使包含换行, 也作为一个整体投递
#    - readline 会忽略 bracket 标记, 但粘贴内容中的 \n 会留在 stdin 缓冲区
#
# 2. 低层级 fd 读取 — 绕过 Python readline 的行缓冲
#    - 用 os.read(fd, N) 而非 sys.stdin.readline()
#    - os.read 直接从内核缓冲区读, 不受 readline 干扰
#
# 3. 自适应超时 — 检测粘贴尾巴
#    - 首次检测: 50ms (粘贴的后续数据应在此窗口内到达)
#    - 后续检测: 10ms (已确认在粘贴中, 加速收集)
#
# 4. Bracketed Paste 标记清理
#    - 某些 readline 版本会透传 \033[200~ 和 \033[201~
#    - 需要从最终文本中剥离

# Bracketed Paste 转义序列
_PASTE_START = "\033[200~"
_PASTE_END = "\033[201~"
_ENABLE_BRACKETED_PASTE = "\033[?2004h"
_DISABLE_BRACKETED_PASTE = "\033[?2004l"

_bracketed_paste_enabled = False


def _enable_bracketed_paste() -> None:
    """启用终端 Bracketed Paste Mode

    效果: 粘贴时终端自动包裹标记, 应用可区分"粘贴"和"键入"。
    即使 readline 本身不支持处理标记, 粘贴内容也会批量到达 stdin。
    """
    global _bracketed_paste_enabled
    if sys.stdout.isatty() and not _bracketed_paste_enabled:
        sys.stdout.write(_ENABLE_BRACKETED_PASTE)
        sys.stdout.flush()
        _bracketed_paste_enabled = True
        atexit.register(_disable_bracketed_paste)


def _disable_bracketed_paste() -> None:
    """退出时恢复终端设置"""
    global _bracketed_paste_enabled
    if _bracketed_paste_enabled:
        try:
            sys.stdout.write(_DISABLE_BRACKETED_PASTE)
            sys.stdout.flush()
        except Exception:
            pass
        _bracketed_paste_enabled = False


def _strip_paste_markers(text: str) -> str:
    """剥离 Bracketed Paste 标记 (某些 readline 版本会透传)"""
    return text.replace(_PASTE_START, "").replace(_PASTE_END, "")


def _drain_stdin(initial_timeout: float = 0.05, burst_timeout: float = 0.01) -> str:
    """从 stdin 低层级读取残余数据 (绕过 readline 缓冲)

    使用 os.read(fd) 直接从内核 fd 读取, 比 sys.stdin.readline() 更可靠:
    - sys.stdin.readline() 经过 Python IO 层 + readline 缓冲, 可能丢数据
    - os.read(fd) 直接读内核缓冲区, 不受 readline 干扰

    Args:
        initial_timeout: 首次探测等待时间 (50ms)
        burst_timeout: 后续读取间隔 (10ms, 已确认有数据时加速)

    Returns:
        残余文本 (空字符串表示无粘贴)
    """
    if not sys.stdin.isatty():
        return ""

    fd = sys.stdin.fileno()
    chunks: list[bytes] = []
    timeout = initial_timeout

    try:
        while True:
            try:
                # select 可能被信号中断（EINTR），需要重试
                ready, _, _ = select.select([fd], [], [], timeout)
            except InterruptedError:
                # 被信号中断，继续等待
                continue
            if not ready:
                break
            data = os.read(fd, 65536)  # 64KB — 足够大以一次性读完大粘贴
            if not data:
                break
            chunks.append(data)
            timeout = burst_timeout  # 首次读到数据后, 切换到短超时快速收尾
    except (OSError, ValueError):
        pass

    if not chunks:
        return ""

    raw = b"".join(chunks)
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        text = raw.decode("latin-1")

    return _strip_paste_markers(text)


def _smart_input(prompt_str: str) -> str:
    """带粘贴检测的智能输入

    工作流程:
    1. readline 处理用户输入的第一行 (input() 返回)
    2. 立即用 _drain_stdin() 从内核 fd 检测残余数据
    3. 如果有残余 → 粘贴, 合并所有行为一个 prompt
    4. 如果无残余 → 普通键入, 直接返回

    特殊处理:
    - 剥离 Bracketed Paste 标记
    - 清理末尾多余空行
    - 显示行数提示
    """
    first_line = input(prompt_str)

    # 剥离可能混入首行的 paste 标记
    first_line = _strip_paste_markers(first_line)

    # 检测粘贴残余
    extra = _drain_stdin()
    if not extra:
        return first_line

    # 合并所有行
    extra_lines = extra.split("\n")

    # 清理: 末尾空行 (粘贴常带一个尾 \n)
    while extra_lines and not extra_lines[-1].strip():
        extra_lines.pop()

    if not extra_lines:
        return first_line

    all_lines = [first_line] + extra_lines
    c = Color
    # 预览粘贴内容 (避免大段输出刷屏, 只显示首尾)
    total = len(all_lines)
    if total <= 5:
        preview = "\n".join(f"  {c.DIM}│{c.RESET} {l}" for l in all_lines)
    else:
        head = "\n".join(f"  {c.DIM}│{c.RESET} {l}" for l in all_lines[:3])
        tail = f"  {c.DIM}│{c.RESET} {all_lines[-1]}"
        preview = f"{head}\n  {c.DIM}│ ... ({total - 4} more lines){c.RESET}\n{tail}"

    print(f"{c.DIM}╭─ 检测到粘贴 ({total} 行):{c.RESET}")
    print(preview)
    print(f"{c.DIM}╰─{c.RESET}")

    return "\n".join(all_lines)


def _read_multiline(end_marker: str = "---") -> str:
    """显式多行输入模式

    三种结束方式:
    1. 输入 '---' (或自定义 end_marker)
    2. 连续两个空行 (快速结束)
    3. Ctrl+D (EOF)

    在此模式下粘贴也能正常工作。
    """
    c = Color
    print(f"{c.DIM}多行输入模式 (输入 {end_marker} 或连按两次 Enter 结束, Ctrl+D 完成):{c.RESET}")
    lines: list[str] = []
    empty_count = 0

    while True:
        try:
            line = input(f"  {c.DIM}│{c.RESET} ")

            # 检测粘贴 (在多行模式下也生效)
            extra = _drain_stdin()
            if extra:
                lines.append(line)
                for el in extra.split("\n"):
                    if el.strip() == end_marker:
                        return "\n".join(lines).strip()
                    lines.append(el)
                empty_count = 0
                continue

            if line.strip() == end_marker:
                break
            if not line.strip():
                empty_count += 1
                if empty_count >= 2:
                    # 移除最后的空行
                    while lines and not lines[-1].strip():
                        lines.pop()
                    break
                lines.append(line)
            else:
                empty_count = 0
                lines.append(line)
        except EOFError:
            break
        except KeyboardInterrupt:
            print(f"\n{c.YELLOW}已取消{c.RESET}")
            return ""

    return "\n".join(lines).strip()


BUILTIN_COMMANDS = [
    "/help", "/status", "/new", "/model", "/mode", "/models",
    "/quit", "/exit", "/kill", "/skill", "/paste",
]

_available_models: Optional[list[str]] = None


def _get_models() -> list[str]:
    """获取可用模型列表 (缓存)"""
    global _available_models
    if _available_models is not None:
        return _available_models

    try:
        result = subprocess.run(
            [AGENT_CLI, "--list-models"],
            capture_output=True, text=True, timeout=10,
        )
        models = []
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if line and " - " in line:
                model_id = line.split(" - ")[0].strip()
                if model_id and not model_id.startswith("Available") and not model_id.startswith("Tip:"):
                    models.append(model_id)
        _available_models = models
        return models
    except Exception:
        return ["sonnet-4.5", "opus-4.6", "gpt-5.2"]


def _completer(text: str, state: int) -> Optional[str]:
    """readline 补全回调"""
    line = readline.get_line_buffer()

    completions = []

    if line.startswith("/model "):
        # 模型补全: 真实模型 + 别名
        prefix = line[7:]
        real_models = [f"/model {m}" for m in _get_models() if m.startswith(prefix)]
        alias_models = [f"/model {a}" for a in sorted(MODEL_ALIASES.keys()) if a.startswith(prefix)]
        completions = real_models + alias_models

    elif line.startswith("/mode "):
        # 模式补全
        prefix = line[6:]
        modes = ["plan", "ask"]
        completions = [f"/mode {m}" for m in modes if m.startswith(prefix)]

    elif line.startswith("/"):
        # 先检查是否是 /skill 后面跟搜索词
        # 或者直接 / 后跟 skill 名
        cmd_parts = line.split(" ", 1)
        cmd = cmd_parts[0]

        if cmd == "/skill" and len(cmd_parts) > 1:
            # /skill <query> 模式
            query = cmd_parts[1]
            matches = _fuzzy_match_skills(query)
            completions = [f"/skill {m['name']}" for m in matches]
        elif cmd in BUILTIN_COMMANDS or cmd == "/skill":
            # 完整命令补全
            completions = [c for c in BUILTIN_COMMANDS if c.startswith(text)]
        else:
            # / + 部分文字 → 先尝试命令匹配, 再尝试 skill 匹配
            query = cmd[1:]  # 去掉 /
            cmd_matches = [c for c in BUILTIN_COMMANDS if c.startswith(cmd)]
            skill_matches = [f"/{m['name']}" for m in _fuzzy_match_skills(query)]
            completions = cmd_matches + skill_matches

    if state < len(completions):
        return completions[state]
    return None


def setup_readline() -> None:
    """配置 readline + 启用 Bracketed Paste Mode"""
    readline.set_completer(_completer)
    readline.set_completer_delims(" \t")
    readline.parse_and_bind("tab: complete")

    # GNU readline 8.1+ 原生支持 bracketed paste
    # 尝试启用 (低版本会忽略)
    try:
        readline.parse_and_bind("set enable-bracketed-paste on")
    except Exception:
        pass

    # 同时在终端层启用 (双保险)
    _enable_bracketed_paste()

    # 尝试加载历史
    history_file = Path.home() / ".acli" / "history"
    history_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        readline.read_history_file(str(history_file))
    except FileNotFoundError:
        pass

    # 限制历史长度
    readline.set_history_length(5000)
    logger.debug("readline configured: history=%s", history_file)


def save_readline_history() -> None:
    history_file = Path.home() / ".acli" / "history"
    try:
        readline.write_history_file(str(history_file))
    except Exception:
        pass
    _disable_bracketed_paste()
    logger.debug("readline history saved")


# ========== 输出渲染 ==========


def render_agent_output(agent: AgentProcess, show_thinking: bool = True) -> str:
    """实时渲染 Agent 输出到终端

    从 JSONL 文件读取事件:
    - assistant 事件 → 正文 (增量 delta)
    - thinking 事件 → 思考过程 (可选)
    - tool_call 事件 → 工具调用信息
    - wait_input.py started → 本轮结束
    """
    c = Color
    accumulated_text = ""
    in_thinking = False
    first_content = True
    event_count = 0
    assistant_chars = 0
    start_ts = time.time()
    outcome = "stream_end"
    logger.info(
        "[%s] render start: read_pos=%s show_thinking=%s",
        agent.session_id,
        agent.read_pos,
        show_thinking,
    )

    for ev, pos in agent.files.read_events(read_pos=agent.read_pos, timeout=600):
        agent.read_pos = pos
        ev_type = ev.get("type", "")
        ev_sub = ev.get("subtype", "")
        event_count += 1
        logger.debug(
            "[%s] render event: idx=%s type=%s subtype=%s pos=%s",
            agent.session_id,
            event_count,
            ev_type,
            ev_sub,
            pos,
        )

        # --- Thinking ---
        if ev_type == "thinking":
            if ev_sub == "delta":
                txt = ev.get("text", "")
                if txt and show_thinking:
                    if not in_thinking:
                        sys.stdout.write(f"\n{c.DIM}{c.CYAN}Thinking...{c.RESET}\n")
                        in_thinking = True
                    sys.stdout.write(f"{c.DIM}{txt}{c.RESET}")
                    sys.stdout.flush()
            continue

        # --- Assistant (正文 delta) ---
        if ev_type == "assistant":
            msg = ev.get("message", {}).get("content", [])
            txt = "".join(
                item.get("text", "") if isinstance(item, dict) else (item if isinstance(item, str) else "")
                for item in msg
            )
            if txt:
                # 过滤累积全文事件
                if _is_cumulative(txt, accumulated_text):
                    logger.debug("[%s] render skip cumulative assistant chunk bytes=%s", agent.session_id, len(txt))
                    continue
                accumulated_text += txt
                assistant_chars += len(txt)

                if in_thinking:
                    # 从 thinking 切换到正文
                    sys.stdout.write(f"{c.RESET}\n\n")
                    in_thinking = False

                if first_content:
                    sys.stdout.write(f"\n{c.GREEN}")
                    first_content = False

                sys.stdout.write(txt)
                sys.stdout.flush()
            continue

        # --- Tool Call ---
        if ev_type == "tool_call":
            if ev_sub == "started":
                cmd = _extract_cmd(ev)
                if "wait_input.py" in cmd:
                    # 本轮结束
                    logger.info("[%s] render stop: wait_input tool started", agent.session_id)
                    outcome = "wait_input_started"
                    break
                if cmd:
                    if in_thinking:
                        sys.stdout.write(f"{c.RESET}\n")
                        in_thinking = False
                    sys.stdout.write(f"\n{c.YELLOW}> {cmd}{c.RESET}\n")
                    sys.stdout.flush()
            elif ev_sub == "result":
                # 工具执行结果 (简短显示)
                output = ev.get("tool_call", {})
                for k, v in output.items():
                    if isinstance(v, dict) and "stdout" in v:
                        stdout = v["stdout"]
                        if stdout:
                            lines = stdout.split("\n")
                            if len(lines) > 10:
                                preview = "\n".join(lines[:5]) + f"\n  ... ({len(lines)} lines total)"
                            else:
                                preview = stdout
                            sys.stdout.write(f"{c.DIM}{preview}{c.RESET}\n")
                            sys.stdout.flush()
            continue

        if ev_type == "acli_internal":
            outcome = ev_sub or "acli_internal"
            logger.warning("[%s] render stop by internal event: subtype=%s", agent.session_id, outcome)
            break

    # 结束输出
    sys.stdout.write(f"{c.RESET}\n")
    sys.stdout.flush()
    logger.info(
        "[%s] render done: events=%s assistant_chars=%s elapsed=%.2fs new_read_pos=%s outcome=%s",
        agent.session_id,
        event_count,
        assistant_chars,
        time.time() - start_ts,
        agent.read_pos,
        outcome,
    )
    return outcome


def _is_cumulative(txt: str, accumulated: str) -> bool:
    """检测是否为累积全文事件（Agent CLI 有时发送完整文本而非增量 delta）。

    判定逻辑：如果新 txt 是已累积文本的前缀或完整匹配，则为累积事件。
    仅当 txt 长度 >= accumulated 的 80% 时才触发（排除恰好重叠的短增量）。
    """
    if not accumulated:
        return False
    if txt == accumulated:
        return True
    if len(txt) >= len(accumulated) * 0.8 and accumulated.startswith(txt):
        return True
    if len(txt) > len(accumulated) and txt.startswith(accumulated):
        return True
    return False


_extract_cmd = extract_tool_command


# ========== REPL 主循环 ==========


def run_repl(
    mgr: SessionManager,
    workspace: str,
    model: str,
    mode: Optional[str] = None,
    api_key: Optional[str] = None,
    one_shot: Optional[str] = None,
) -> None:
    """启动 REPL 主循环

    Args:
        mgr: SessionManager 实例
        workspace: 工作目录
        model: 初始模型
        mode: Agent 模式 (plan/ask, 可选)
        api_key: API key (可选)
        one_shot: 单次执行模式, 执行后退出
    """
    c = Color
    setup_readline()
    logger.info(
        "run_repl start: workspace=%s model=%s mode=%s one_shot=%s",
        workspace,
        model,
        mode,
        bool(one_shot),
    )

    current_model = model
    current_mode = mode

    # one-shot 模式
    if one_shot:
        logger.info("run_repl one_shot execute: bytes=%s", len(one_shot or ""))
        _execute_prompt(mgr, workspace, current_model, current_mode, api_key, one_shot)
        return

    # 欢迎信息
    _print_banner(workspace, current_model)

    while True:
        try:
            # 构建提示符
            dir_name = os.path.basename(workspace)
            prompt_str = f"{c.BLUE}{dir_name}{c.RESET} {c.MAGENTA}({current_model}){c.RESET} > "
            user_input = _smart_input(prompt_str)

        except (EOFError, KeyboardInterrupt):
            logger.info("run_repl user exit by EOF/KeyboardInterrupt")
            print(f"\n{c.DIM}Bye!{c.RESET}")
            save_readline_history()
            return

        user_input = user_input.strip()
        if not user_input:
            continue
        logger.debug("run_repl input received: bytes=%s startswith_slash=%s", len(user_input), user_input.startswith("/"))

        # 多行输入支持 (末尾 \ 续行)
        while user_input.endswith("\\"):
            try:
                cont = input(f"{c.DIM}... {c.RESET}")
                user_input = user_input[:-1] + "\n" + cont
            except (EOFError, KeyboardInterrupt):
                break

        # ---- 处理命令 ----
        if user_input.startswith("/"):
            handled = _handle_command(
                user_input, mgr, workspace,
                current_model, current_mode, api_key,
            )
            if handled == "__quit__":
                logger.info("run_repl command requested quit")
                save_readline_history()
                return
            elif handled == "__kill__":
                logger.info("run_repl command requested kill")
                mgr.kill_session(workspace, current_model)
                print(f"{c.YELLOW}当前 Agent 已终止{c.RESET}")
                save_readline_history()
                return
            elif isinstance(handled, dict):
                # 命令更新了状态
                if "model" in handled:
                    current_model = handled["model"]
                    logger.info("run_repl model changed: %s", current_model)
                    print(f"{c.GREEN}模型切换为: {current_model}{c.RESET}")
                if "mode" in handled:
                    current_mode = handled["mode"]
                    logger.info("run_repl mode changed: %s", current_mode)
                    print(f"{c.GREEN}模式切换为: {current_mode or 'default'}{c.RESET}")
                if "prompt" in handled:
                    logger.info("run_repl command generated prompt: bytes=%s", len(handled["prompt"] or ""))
                    # 命令产生了一个 prompt (如 /skill)
                    _execute_prompt(
                        mgr, workspace, current_model, current_mode,
                        api_key, handled["prompt"],
                    )
            continue

        # ---- 普通 prompt ----
        _execute_prompt(mgr, workspace, current_model, current_mode, api_key, user_input)


def _execute_prompt(
    mgr: SessionManager,
    workspace: str,
    model: str,
    mode: Optional[str],
    api_key: Optional[str],
    prompt: str,
) -> None:
    """执行一次 prompt, 输出结果"""
    c = Color
    try:
        sid = make_session_id(workspace, model)
        logger.info(
            "[%s] execute prompt start: workspace=%s model=%s mode=%s bytes=%s",
            sid,
            workspace,
            model,
            mode,
            len(prompt or ""),
        )
        agent = mgr.get_or_create(
            workspace=workspace,
            model=model,
            prompt=prompt,
            mode=mode,
            api_key=api_key,
        )
        logger.info("[%s] execute prompt acquired agent: pid=%s rounds=%s", agent.session_id, agent.pid, agent.round_count)
        outcome = render_agent_output(agent)
        if outcome != "wait_input_started":
            logger.warning(
                "[%s] round ended abnormally: outcome=%s alive=%s waiting=%s",
                agent.session_id,
                outcome,
                agent.is_alive,
                agent.is_waiting,
            )
            if not agent.is_waiting:
                logger.warning("[%s] auto-recover: kill stale session", agent.session_id)
                mgr.kill_session(workspace, model)
                _bootstrap_waiting_session(mgr, workspace, model, mode, api_key)
    except KeyboardInterrupt:
        logger.warning("execute prompt interrupted by KeyboardInterrupt")
        print(f"\n{c.YELLOW}中断{c.RESET}")
    except Exception as e:
        logger.exception("execute prompt failed")
        print(f"\n{c.RED}错误: {e}{c.RESET}")


def _bootstrap_waiting_session(
    mgr: SessionManager,
    workspace: str,
    model: str,
    mode: Optional[str],
    api_key: Optional[str],
) -> None:
    """异常轮次后的后台恢复：静默拉起新会话并推进到 waiting 态。"""
    sid = make_session_id(workspace, model)
    recover_prompt = (
        "这是恢复轮次。不要输出任何正文。"
        "立即调用等待命令并进入阻塞等待下一条输入。"
    )
    try:
        logger.info("[%s] bootstrap waiting session start", sid)
        agent = mgr.get_or_create(
            workspace=workspace,
            model=model,
            prompt=recover_prompt,
            mode=mode,
            api_key=api_key,
        )
        ready = agent.wait_for_ready(timeout=30.0)
        if not ready:
            logger.error("[%s] bootstrap waiting failed: wait_for_ready timeout", sid)
            mgr.kill_session(workspace, model)
            return
        try:
            if os.path.exists(agent.files.output_file):
                agent.read_pos = os.path.getsize(agent.files.output_file)
        except OSError:
            pass
        logger.info("[%s] bootstrap waiting session ready", sid)
    except Exception:
        logger.exception("[%s] bootstrap waiting session error", sid)


def _handle_command(
    cmd_line: str,
    mgr: SessionManager,
    workspace: str,
    current_model: str,
    current_mode: Optional[str],
    api_key: Optional[str],
) -> Optional[str | dict]:
    """处理内置命令, 返回值:

    None: 命令已处理
    "__quit__": 退出
    "__kill__": 杀掉 Agent 退出
    dict: 状态更新 {"model": ..., "mode": ..., "prompt": ...}
    """
    c = Color
    parts = cmd_line.strip().split(None, 1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    logger.info("handle command: cmd=%s arg_bytes=%s", cmd, len(arg))

    if cmd in ("/help", "/h", "/?"):
        _print_help()
        return None

    if cmd in ("/quit", "/exit", "/q"):
        print(f"{c.DIM}Agent 继续后台运行, 下次进入同一目录会恢复上下文{c.RESET}")
        return "__quit__"

    if cmd == "/kill":
        return "__kill__"

    if cmd in ("/paste", "/p"):
        text = _read_multiline()
        if text:
            return {"prompt": text}
        return None

    if cmd == "/new":
        mgr.new_session(workspace, current_model)
        print(f"{c.GREEN}上下文已清除, 下次输入将启动新 Agent{c.RESET}")
        return None

    if cmd == "/status":
        sessions = mgr.list_sessions()
        if not sessions:
            print(f"{c.DIM}没有活跃的会话{c.RESET}")
        else:
            print(f"\n{c.BOLD}活跃会话:{c.RESET}")
            for s in sessions:
                marker = f"{c.GREEN}●{c.RESET}" if s["alive"] else f"{c.RED}●{c.RESET}"
                state = "已退出" if not s["alive"] else ("等待中" if s["waiting"] else "运行中")
                wd = os.path.basename(s["workspace"])
                print(f"  {marker} {wd} ({s['model']}) - {state} "
                      f"| rounds={s['rounds']} idle={s['idle_min']}min pid={s['pid']}")
            print()
        return None

    if cmd == "/model":
        if not arg:
            print(f"{c.DIM}当前模型: {current_model}{c.RESET}")
            print(f"{c.DIM}用法: /model <name>  (支持别名: opus-thinking, sonnet, codex ...){c.RESET}")
            return None
        # 解析模型别名/模糊匹配
        resolved, hint = resolve_model(arg)
        if hint:
            print(f"{c.CYAN}{hint}{c.RESET}")
        # 切换模型 = 新的 session
        return {"model": resolved}

    if cmd == "/mode":
        if not arg:
            print(f"{c.DIM}当前模式: {current_mode or 'default'}{c.RESET}")
            print(f"{c.DIM}可用: plan, ask (留空为默认){c.RESET}")
            return None
        if arg == "default":
            return {"mode": None}
        return {"mode": arg}

    if cmd == "/models":
        models = _get_models()
        print(f"\n{c.BOLD}可用模型:{c.RESET}")
        for m in models:
            marker = f"{c.GREEN}*{c.RESET}" if m == current_model else " "
            print(f"  {marker} {m}")
        print()
        return None

    if cmd == "/skill":
        if not arg:
            # 列出所有 skills
            skills = _load_skills()
            if not skills:
                print(f"{c.YELLOW}未找到 skill{c.RESET}")
            else:
                print(f"\n{c.BOLD}可用 Skills:{c.RESET}")
                for s in skills:
                    print(f"  {c.CYAN}{s['name']}{c.RESET}")
                print(f"\n{c.DIM}用法: /skill <name> 或 /<name> + Tab 自动补全{c.RESET}")
            return None
        # 查找 skill
        matches = _fuzzy_match_skills(arg)
        if not matches:
            print(f"{c.RED}未找到匹配的 skill: {arg}{c.RESET}")
            return None
        skill = matches[0]
        print(f"{c.GREEN}加载 skill: {skill['name']}{c.RESET}")
        print(f"{c.DIM}输入任务描述 (skill 将指导 Agent 的工作流):{c.RESET}")
        try:
            task = input(f"{c.BLUE}task> {c.RESET}")
        except (EOFError, KeyboardInterrupt):
            return None
        if not task.strip():
            print(f"{c.YELLOW}已取消{c.RESET}")
            return None
        # 构建 skill prompt
        skill_prompt = (
            f"请先阅读以下 skill 文件并遵循其中的工作流程:\n"
            f"{skill['path']}\n\n"
            f"然后执行以下任务:\n{task.strip()}"
        )
        return {"prompt": skill_prompt}

    # / + skill 名称 快捷方式
    if cmd.startswith("/"):
        query = cmd[1:]
        matches = _fuzzy_match_skills(query)
        if matches and len(matches) == 1:
            skill = matches[0]
            # 如果有 arg 直接用, 否则提示输入
            if arg:
                task = arg
            else:
                print(f"{c.GREEN}加载 skill: {skill['name']}{c.RESET}")
                try:
                    task = input(f"{c.BLUE}task> {c.RESET}")
                except (EOFError, KeyboardInterrupt):
                    return None
                if not task.strip():
                    print(f"{c.YELLOW}已取消{c.RESET}")
                    return None

            skill_prompt = (
                f"请先阅读以下 skill 文件并遵循其中的工作流程:\n"
                f"{skill['path']}\n\n"
                f"然后执行以下任务:\n{task.strip()}"
            )
            return {"prompt": skill_prompt}
        elif matches and len(matches) > 1:
            print(f"{c.YELLOW}多个匹配, 请更精确:{c.RESET}")
            for m in matches[:10]:
                print(f"  {c.CYAN}{m['name']}{c.RESET}")
            return None

    print(f"{c.RED}未知命令: {cmd}{c.RESET}")
    print(f"{c.DIM}输入 /help 查看可用命令{c.RESET}")
    return None


# ========== UI ==========


def _print_banner(workspace: str, model: str) -> None:
    c = Color
    print(f"""
{c.BOLD}{c.BLUE}╔══════════════════════════════════════╗
║         acli — Agent CLI REPL        ║
╚══════════════════════════════════════╝{c.RESET}
{c.DIM}Workspace:{c.RESET} {workspace}
{c.DIM}Model:{c.RESET}     {model}
{c.DIM}Commands:{c.RESET}  /help  /status  /skill  /new  /quit
{c.DIM}Tab 补全:{c.RESET}  / + Tab 列出命令和 skill
""")


def _print_help() -> None:
    c = Color
    print(f"""
{c.BOLD}内置命令:{c.RESET}
  {c.CYAN}/help{c.RESET}              显示此帮助
  {c.CYAN}/status{c.RESET}            列出所有活跃 session
  {c.CYAN}/new{c.RESET}               清除上下文，重新开始
  {c.CYAN}/model <name>{c.RESET}      切换模型 (支持别名 + Tab 补全)
  {c.CYAN}/mode <plan|ask>{c.RESET}   切换 Agent 模式
  {c.CYAN}/models{c.RESET}            列出所有可用模型
  {c.CYAN}/skill <name>{c.RESET}      加载 skill 并输入任务
  {c.CYAN}/<skill_name>{c.RESET}      快捷 skill 调用 (Tab 模糊匹配)
  {c.CYAN}/paste{c.RESET}             多行输入模式 (空行或 --- 结束)
  {c.CYAN}/quit{c.RESET}              退出 (Agent 继续后台运行)
  {c.CYAN}/kill{c.RESET}              退出并杀死当前 Agent

{c.BOLD}模型别名:{c.RESET}
  {c.DIM}opus-thinking{c.RESET}  → opus-4.6-thinking    {c.DIM}sonnet{c.RESET}  → sonnet-4.5
  {c.DIM}opus{c.RESET}           → opus-4.6              {c.DIM}codex{c.RESET}   → gpt-5.3-codex
  {c.DIM}gemini{c.RESET}         → gemini-3-pro          {c.DIM}flash{c.RESET}   → gemini-3-flash

{c.BOLD}多行输入:{c.RESET}
  粘贴:   自动检测多行粘贴, 合并为一个 prompt
  /paste:  显式多行输入模式 (空行或 --- 结束)
  行末 \\: 续行 (手动拼接)

{c.BOLD}特殊:{c.RESET}
  Tab:      命令/模型/skill 自动补全
""")
