"""
acli 配置常量
"""
import os
from pathlib import Path

# ========== Agent CLI ==========
AGENT_CLI = os.environ.get(
    "ACLI_AGENT_CLI",
    os.environ.get("CAG_AGENT_CLI", str(Path.home() / ".local" / "bin" / "agent")),
)

# ========== IPC ==========
# 运行时 IPC 目录（会话间的临时通信）
IPC_RUNTIME_DIR = Path(
    os.environ.get(
        "ACLI_IPC_RUNTIME_DIR",
        os.environ.get(
            "ACLI_IPC_DIR",  # 向后兼容旧变量名
            str(Path.home() / ".acli" / "ipc"),  # 更安全，系统重启后可恢复
        ),
    )
)
# 向后兼容：旧代码可能使用 IPC_DIR
IPC_DIR = IPC_RUNTIME_DIR
IPC_PREFIX = "acli_"
IPC_BACKEND_REQUESTED = os.environ.get("ACLI_IPC_BACKEND", "sqlite").strip().lower() or "sqlite"
IPC_BACKEND = "sqlite"  # sqlite-only
IPC_BACKEND_FORCED = IPC_BACKEND_REQUESTED != "sqlite"

# ========== 数据格式版本 ==========
# 用于会话元数据的 JSON 序列化版本
SESSION_DB_SCHEMA_VERSION = 1

# ========== 会话 ==========
# 最多 24 小时（后台支持的最长超时）
SESSION_IDLE_TIMEOUT_HOURS = int(os.environ.get("ACLI_IDLE_HOURS", "24"))
SESSION_IDLE_TIMEOUT_SECS = min(SESSION_IDLE_TIMEOUT_HOURS, 24) * 3600  # 最多 24 小时
SESSION_DB_DIR = Path(os.environ.get(
    "ACLI_SESSION_DIR",
    str(Path.home() / ".acli" / "sessions"),
))

# ========== Skill ==========
SKILL_DIRS = [
    Path.home() / ".cursor" / "skills",
    Path.home() / ".cursor" / "skills-cursor",
]

# ========== 默认模型 ==========
DEFAULT_MODEL = os.environ.get("ACLI_DEFAULT_MODEL", "sonnet-4.5")

# ========== 模型别名 (短名 → Agent CLI model ID) ==========
# 用户可以用简短的别名, 自动解析为完整 model ID
MODEL_ALIASES = {
    # Claude 系列
    "opus":             "opus-4.6",
    "opus-thinking":    "opus-4.6-thinking",
    "opus46":           "opus-4.6",
    "opus46t":          "opus-4.6-thinking",
    "opus45":           "opus-4.5",
    "opus45t":          "opus-4.5-thinking",
    "sonnet":           "sonnet-4.5",
    "sonnet-thinking":  "sonnet-4.5-thinking",
    "sonnet45":         "sonnet-4.5",
    "sonnet45t":        "sonnet-4.5-thinking",
    # GPT 系列
    "gpt":              "gpt-5.2",
    "gpt52":            "gpt-5.2",
    "gpt53":            "gpt-5.3-codex",
    "codex":            "gpt-5.3-codex",
    "codex-high":       "gpt-5.3-codex-high",
    "codex-fast":       "gpt-5.3-codex-fast",
    # Gemini / Grok
    "gemini":           "gemini-3-pro",
    "flash":            "gemini-3-flash",
    "grok":             "grok",
    # 特殊
    "auto":             "auto",
}

# 常用模型 (在 --help 中展示)
POPULAR_MODELS = [
    ("opus-4.6-thinking", "Claude 4.6 Opus (Thinking) — 最强推理"),
    ("opus-4.6",          "Claude 4.6 Opus"),
    ("sonnet-4.5",        "Claude 4.5 Sonnet — 默认"),
    ("sonnet-4.5-thinking", "Claude 4.5 Sonnet (Thinking)"),
    ("gpt-5.3-codex",    "GPT-5.3 Codex"),
    ("gpt-5.2",          "GPT-5.2"),
    ("gemini-3-pro",     "Gemini 3 Pro"),
    ("auto",             "自动选择"),
]

# ========== 模型解析 ==========
_all_models_cache: list[str] | None = None


def _fetch_all_models() -> list[str]:
    """从 Agent CLI 获取所有可用模型 ID (带缓存)"""
    global _all_models_cache
    if _all_models_cache is not None:
        return _all_models_cache
    import subprocess
    try:
        result = subprocess.run(
            [AGENT_CLI, "--list-models"],
            capture_output=True, text=True, timeout=10,
        )
        models = []
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if line and " - " in line:
                mid = line.split(" - ")[0].strip()
                if mid and not mid.startswith("Available") and not mid.startswith("Tip:"):
                    models.append(mid)
        _all_models_cache = models
        return models
    except Exception:
        return []


def resolve_model(user_input: str) -> tuple[str, str | None]:
    """解析用户输入的模型名 → (model_id, hint_message)

    解析优先级:
    1. 精确匹配 Agent CLI 已知模型
    2. 别名匹配 (MODEL_ALIASES)
    3. 模糊匹配 (前缀 → 子串)
    4. 原样返回 (让 Agent CLI 自己报错)

    Returns:
        (resolved_model_id, hint_or_None)
    """
    raw = user_input.strip().lower()

    # 1. 精确匹配
    all_models = _fetch_all_models()
    if raw in [m.lower() for m in all_models]:
        # 返回原始大小写
        for m in all_models:
            if m.lower() == raw:
                return m, None
        return user_input, None

    # 2. 别名
    if raw in MODEL_ALIASES:
        resolved = MODEL_ALIASES[raw]
        return resolved, f"别名 '{user_input}' → {resolved}"

    # 3. 模糊匹配: 前缀
    prefix = [m for m in all_models if m.lower().startswith(raw)]
    if len(prefix) == 1:
        return prefix[0], f"模糊匹配 '{user_input}' → {prefix[0]}"
    if len(prefix) > 1:
        # 多个匹配, 返回第一个但提示
        hint = f"多个匹配: {', '.join(prefix[:5])}... 使用: {prefix[0]}"
        return prefix[0], hint

    # 4. 子串匹配
    substr = [m for m in all_models if raw in m.lower()]
    if len(substr) == 1:
        return substr[0], f"模糊匹配 '{user_input}' → {substr[0]}"
    if len(substr) > 1:
        hint = f"多个匹配: {', '.join(substr[:5])}... 使用: {substr[0]}"
        return substr[0], hint

    # 5. 别名 key 模糊匹配
    alias_matches = [k for k in MODEL_ALIASES if raw in k]
    if alias_matches:
        resolved = MODEL_ALIASES[alias_matches[0]]
        return resolved, f"别名匹配 '{user_input}' → {resolved}"

    # 6. 原样返回
    return user_input, f"未识别的模型 '{user_input}', 原样传递给 Agent CLI"


# ========== 轮询间隔 (秒) ==========
POLL_FAST = 0.02     # 20ms — 有数据时
POLL_SLOW = 0.1      # 100ms — 空闲时
POLL_INPUT = 0.05    # 50ms — wait_input.py 轮询

# result=success 后等待 wait_input started 的兜底窗口（秒）
WAIT_INPUT_AFTER_RESULT_GRACE_SECS = float(
    os.environ.get("ACLI_WAIT_INPUT_AFTER_RESULT_GRACE_SECS", "12")
)

# 每轮用户输入自动附加“二次提醒”
SECOND_REMINDER_ENABLED = os.environ.get("ACLI_SECOND_REMINDER_ENABLED", "1").strip() not in (
    "0",
    "false",
    "False",
)
SECOND_REMINDER_TEMPLATE = (
    "\n\n[ACLI_WORKFLOW_REMINDER]\n"
    "这是流程提醒，不要在回复中复述。\n"
    "本轮回复结束后，你必须立刻调用以下命令进入等待：\n"
    "python3 {wait_script} {session_id} {timeout}\n"
    "调用后保持等待，不要主动结束进程。"
)

# ========== 持久化 Prompt 模板 ==========
PERSISTENT_PROMPT_TEMPLATE = """你是一个持久化循环助手。遵守以下工作流程：

【工作流程】
1. 执行用户的当前指令，输出完整回复
2. 回复结束后，立即调用 Shell 工具执行等待命令（timeout 设为 {timeout}）：
   python3 {wait_script} {session_id} {timeout}
3. 等待命令返回下一轮用户指令
4. 收到新指令后直接执行，重复步骤 1-2-3
5. 永不主动退出

【重要规则】
- 直接回复用户的问题/指令，不要输出额外的总结
- 回复结束后立刻调用等待命令，不要解释你要做什么，不要输出 "现在调用等待命令" 之类的话
- 等待命令返回的文本就是下一轮指令，直接执行
- 如果指令要求创建文件或执行命令，使用对应工具完成
- 当前工作目录是: {workspace}

当前指令：{user_prompt}"""

# ========== 颜色 (ANSI) ==========
class Color:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    RED     = "\033[31m"
    GREEN   = "\033[32m"
    YELLOW  = "\033[33m"
    BLUE    = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN    = "\033[36m"
    WHITE   = "\033[37m"
    # 背景
    BG_BLUE = "\033[44m"
