# acli — Agent CLI REPL

轻量级命令行工具，复用 Cursor Agent CLI 的持久会话机制，为每个工作目录维护独立的 Agent 实例。

## 核心特性

- **零冷启动**: 复用持久化 Agent 进程，后续交互秒级响应（首次 ~10s，后续 ~3s）
- **目录隔离**: 每个 (工作目录 + 模型) 组合独立会话，互不干扰
- **自动恢复**: 退出 REPL 后 Agent 继续后台运行，下次进入自动恢复上下文
- **模型别名**: `opus-thinking` → `opus-4.6-thinking`，支持模糊匹配
- **Skill 集成**: `/skill` + Tab 模糊匹配 `~/.cursor/skills` 下的工作流
- **多行粘贴**: 自动检测粘贴的多行文本，合并为一个 prompt
- **Tab 补全**: 命令、模型名、别名、skill 名全部支持 Tab 自动补全
- **10 小时空闲超时**: Agent 无活动 10 小时后自动清理

## 安装

### 前提条件

- **Python >= 3.9** (仅标准库，无外部依赖)
- **Cursor Agent CLI** — 安装并登录 (`agent login`)

### 设置 alias

```bash
# 方式 1: alias (推荐)
echo 'alias acli="python3 /path/to/acli/acli.py"' >> ~/.zshrc
source ~/.zshrc

# 方式 2: symlink
ln -s /path/to/acli/acli.py /usr/local/bin/acli
chmod +x /path/to/acli/acli.py
```

## 快速开始

```bash
# 在任意项目目录下启动交互式 REPL
cd /your/project
acli

# 使用 Opus 4.6 Thinking (最强推理)
acli -m opus-thinking

# 单次执行
acli -p "解释这个项目的架构"

# 规划模式 (只读分析)
acli --mode plan
```

## 用法

### 交互式 REPL (主要模式)

```bash
acli                              # 默认模型 (sonnet-4.5)
acli -m opus-thinking             # Opus 4.6 Thinking (别名)
acli -m opus-4.6-thinking         # 同上 (全名)
acli -m sonnet-thinking           # Sonnet 4.5 Thinking
acli -m codex                     # GPT-5.3 Codex
acli --mode plan                  # 规划模式 (只读分析)
acli --mode ask                   # 问答模式 (只读)
```

### 单次执行 (-p)

```bash
acli -p "解释这个项目的架构"
acli -p "写一个 hello world" -m gpt
acli -p "分析性能瓶颈" -m opus-thinking --mode plan
```

### 管理命令

```bash
acli status                       # 查看所有活跃会话
acli models                       # 列出所有可用模型
acli kill                         # 杀掉当前目录的 Agent
acli kill --all                   # 杀掉所有 Agent
acli cleanup                      # 清理所有 Agent 和临时文件
```

## 模型别名

支持简短别名，自动解析为完整 model ID：

| 别名 | 解析为 | 说明 |
|------|--------|------|
| `opus-thinking` | `opus-4.6-thinking` | Claude 4.6 Opus (Thinking) — 最强推理 |
| `opus` | `opus-4.6` | Claude 4.6 Opus |
| `sonnet` | `sonnet-4.5` | Claude 4.5 Sonnet — 默认 |
| `sonnet-thinking` | `sonnet-4.5-thinking` | Claude 4.5 Sonnet (Thinking) |
| `codex` | `gpt-5.3-codex` | GPT-5.3 Codex |
| `gpt` | `gpt-5.2` | GPT-5.2 |
| `gemini` | `gemini-3-pro` | Gemini 3 Pro |
| `flash` | `gemini-3-flash` | Gemini 3 Flash |
| `auto` | `auto` | 自动选择 |

还支持模糊匹配: `thinking` → `opus-4.6-thinking`, `gpt-5.3` → `gpt-5.3-codex`

## REPL 内置命令

| 命令 | 作用 |
|------|------|
| `/help` | 显示帮助 |
| `/status` | 列出所有活跃 session |
| `/new` | 清除当前上下文，重新开始 |
| `/model <name>` | 切换模型 (支持别名 + Tab 补全) |
| `/mode <plan\|ask>` | 切换 Agent 模式 |
| `/models` | 列出所有可用模型 |
| `/skill <name>` | 加载 skill 并输入任务 |
| `/<skill_name>` | 快捷 skill 调用 (Tab 模糊匹配) |
| `/paste` | 多行输入模式 (空行或 `---` 结束) |
| `/quit` | 退出 (Agent 继续后台运行) |
| `/kill` | 退出并杀死当前 Agent |

## 多行输入

三种方式处理多行文本：

```bash
# 1. 直接粘贴 (自动检测，最常用)
> [粘贴多行文本]
(检测到粘贴: 5 行)

# 2. /paste 命令 (显式多行输入)
> /paste
多行输入模式 (空行或 --- 结束, Ctrl+D 完成):
  | 第一行内容
  | 第二行内容
  | ---

# 3. 末尾 \ 续行
> 第一行\
... 第二行
```

## Skill 系统

自动扫描 `~/.cursor/skills/` 和 `~/.cursor/skills-cursor/` 下的 skill 目录。

```
> /brain[Tab]                    → 补全为 /brainstorming
> /brainstorming
✓ 加载 skill: brainstorming
task> 设计一个缓存系统

# 也可以用 /skill 命令:
> /skill debug
✓ 加载 skill: systematic-debugging
task> 分析这个 bug
```

选中 skill 后，Agent 会先阅读该 skill 的 `SKILL.md` 文件，然后按照其中的工作流执行你的任务。

## 架构

```
acli/
├── acli.py              主入口 (argparse + 子命令)
├── acli_gateway.py      HTTP Gateway (OpenAI + Anthropic/Claude Code)
├── config.py            配置常量 + 模型别名 + Prompt 模板
├── gateway_helpers.py   Gateway 通用逻辑 (workspace/session/prompt 解析)
├── session_manager.py   会话管理 (per-directory isolation)
├── agent_process.py     Agent CLI 进程管理 + 孤儿清理
├── ipc.py               文件 IPC 通信 (JSONL)
├── repl.py              REPL (readline + 补全 + 粘贴检测 + 渲染)
├── wait_input.py        Agent 等待脚本 (被 Agent Shell 工具调用)
├── gateway_requirements.txt Gateway 依赖
├── start_claude_gateway.sh  一键启动 Gateway + Claude Code
├── requirements.txt     依赖说明 (纯标准库)
└── README.md            本文件
```

### 工作原理

```
┌──────────────┐         ┌──────────────┐         ┌──────────────┐
│ 用户终端      │         │  acli REPL    │         │ Agent CLI    │
│              │  input  │              │  IPC 文件 │ (Node.js)    │
│  > 你好      │────────▶│ session_mgr  │────────▶│              │
│              │◀────────│ render_output│◀────────│ stream-json  │
│  Agent: ...  │ stdout  │              │  JSONL   │              │
└──────────────┘         └──────────────┘         └──────────────┘
                                                         │
                                                         ▼
                                                  ┌──────────────┐
                                                  │ Cursor Cloud  │
                                                  │ (AI Models)   │
                                                  └──────────────┘
```

1. 用户输入 → REPL 写入 SQLite 队列 → `wait_input.py` 读取并传给 Agent
2. Agent 调用 AI 模型 → 输出 stream-json 到 JSONL 文件
3. REPL 实时轮询 JSONL → 渲染到终端
4. Agent 完成回复 → 调用 `wait_input.py` 等待下一轮

### IPC 文件

```
~/.acli/ipc/acli_out_{session_id}.jsonl      Agent 输出 (stream-json)
~/.acli/ipc/acli_err_{session_id}.err        Agent stderr
~/.acli/ipc/acli_prompt_{session_id}.txt     首轮 prompt
~/.acli/ipc/acli_waiting_{session_id}.marker wait_input.py 就绪标记
~/.acli/ipc/acli_ipc.sqlite3                 SQLite IPC 队列/状态库 (默认)
```

### 持久化数据

```
~/.acli/sessions/{session_id}.json    会话元数据 (用于跨进程恢复)
~/.acli/history                       readline 历史
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ACLI_AGENT_CLI` | `~/.local/bin/agent` | Agent CLI 路径 |
| `ACLI_DEFAULT_MODEL` | `sonnet-4.5` | 默认模型 |
| `ACLI_IDLE_HOURS` | `24` | 空闲超时 (小时, 上限 24) |
| `ACLI_IPC_BACKEND` | `sqlite` | 已固定为 `sqlite`（仅兼容读取旧配置） |
| `ACLI_IPC_RUNTIME_DIR` | `~/.acli/ipc` | IPC 运行目录 |
| `ACLI_IPC_DIR` | `~/.acli/ipc` | 旧变量名（兼容） |
| `ACLI_SESSION_DIR` | `~/.acli/sessions` | 会话元数据目录 |
| `ACLI_CONSOLE_LOG_LEVEL` | `ERROR` | 控制台日志级别（文件日志不受影响） |
| `ACLI_GATEWAY_HOST` | `0.0.0.0` | gateway 监听地址 |
| `ACLI_GATEWAY_PORT` | `8080` | gateway 监听端口 |
| `ACLI_GATEWAY_WORKSPACE` | 当前目录 | gateway 默认工作目录 |
| `ACLI_GATEWAY_DEFAULT_MODEL` | `sonnet-4.5` | gateway 默认模型 |
| `ACLI_GATEWAY_MAX_CONCURRENT` | `5` | gateway 最大并发请求数 |

## Gateway (Claude Code)

`acli_gateway.py` 复用当前 `acli` 的持久化 prompt/workflow，不额外维护另一套模板。

### 安装依赖

```bash
pip3 install -r gateway_requirements.txt
```

### 启动 Gateway（指定端口）

```bash
python3 acli_gateway.py --port 8080
```

可选参数：

```bash
python3 acli_gateway.py \
  --host 0.0.0.0 \
  --port 8080 \
  --workspace /path/to/project \
  --model sonnet-4.5 \
  --max-concurrent 5 \
  --agent-cli ~/.local/bin/agent
```

### 一键启动 Gateway + Claude Code

```bash
./start_claude_gateway.sh 8080
```

脚本行为：
1. 启动 `acli_gateway.py`
2. 健康检查通过后自动启动 `claude`
3. 自动设置 `ANTHROPIC_BASE_URL=http://127.0.0.1:8080`
4. `claude` 退出时自动回收 gateway

### 会话规划策略

- Session 由 `workspace + model` 生成（与 `acli` 一致）
- `workspace` 提取优先级：
  - `X-Workspace-Path` / `X-Workspace` / `X-Project-Path` 请求头
  - 请求 `metadata` 中的 `cwd/workspace/workspace_path/project_path/path/root`
  - gateway 启动时的默认 `--workspace`
- 因此可以按 Claude Code 启动项目路径进行会话隔离

## 与 Gateway 的区别

| 特性 | acli | gateway |
|------|------|---------|
| 用途 | 命令行交互 | HTTP API 服务 |
| 接口 | 终端 REPL | OpenAI / Anthropic API |
| 客户端 | 直接使用 | curl / SDK / Claude Code |
| 隔离 | 按工作目录 | 可按 workspace + model |
| 依赖 | 纯标准库 | FastAPI + uvicorn |
