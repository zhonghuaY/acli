import os
import importlib
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


class WaitInputRegressionTests(unittest.TestCase):
    def test_wait_input_reads_prequeued_message(self) -> None:
        session_id = f"reg_{int(time.time() * 1000)}"

        with tempfile.TemporaryDirectory(prefix="acli_ipc_test_") as td:
            env = os.environ.copy()
            env["ACLI_IPC_RUNTIME_DIR"] = str(td)

            enqueue = subprocess.run(
                [
                    "python3",
                    "-c",
                    (
                        "from ipc import SessionFiles;"
                        f"s=SessionFiles('{session_id}');"
                        "s.send_input('HELLO_FROM_TEST')"
                    ),
                ],
                capture_output=True,
                text=True,
                env=env,
                timeout=5,
            )
            self.assertEqual(enqueue.returncode, 0, enqueue.stderr)

            result = subprocess.run(
                ["python3", "wait_input.py", session_id, "1"],
                capture_output=True,
                text=True,
                env=env,
                timeout=5,
            )

            self.assertEqual(result.returncode, 0)
            self.assertIn("HELLO_FROM_TEST", result.stdout)

    def test_sqlite_backend_preserves_fifo_for_multiple_messages(self) -> None:
        session_id = f"fifo_{int(time.time() * 1000)}"

        with tempfile.TemporaryDirectory(prefix="acli_ipc_sqlite_") as td:
            env = os.environ.copy()
            env["ACLI_IPC_RUNTIME_DIR"] = td

            enqueue = subprocess.run(
                [
                    "python3",
                    "-c",
                    (
                        "from ipc import SessionFiles;"
                        f"s=SessionFiles('{session_id}');"
                        "s.send_input('M1');"
                        "s.send_input('M2')"
                    ),
                ],
                capture_output=True,
                text=True,
                env=env,
                timeout=5,
            )
            self.assertEqual(enqueue.returncode, 0, enqueue.stderr)

            first = subprocess.run(
                ["python3", "wait_input.py", session_id, "1"],
                capture_output=True,
                text=True,
                env=env,
                timeout=5,
            )
            self.assertEqual(first.returncode, 0)
            self.assertIn("M1", first.stdout)

            second = subprocess.run(
                ["python3", "wait_input.py", session_id, "1"],
                capture_output=True,
                text=True,
                env=env,
                timeout=5,
            )
            self.assertEqual(second.returncode, 0)
            self.assertIn("M2", second.stdout)


class RestoredSessionAliveRegressionTests(unittest.TestCase):
    def test_restored_session_with_live_pid_is_alive(self) -> None:
        from agent_process import AgentProcess

        agent = AgentProcess("acli_test", "/tmp", "sonnet-4.5")
        agent.proc = None
        agent.pid = os.getpid()
        agent.created_at = time.time() - 2

        self.assertTrue(agent.is_alive)


class MarkerValidationRegressionTests(unittest.TestCase):
    def test_dead_pid_marker_is_not_waiting(self) -> None:
        with tempfile.TemporaryDirectory(prefix="acli_ipc_marker_") as td:
            old_runtime = os.environ.get("ACLI_IPC_RUNTIME_DIR")
            old_backend = os.environ.get("ACLI_IPC_BACKEND")
            os.environ["ACLI_IPC_RUNTIME_DIR"] = td
            os.environ["ACLI_IPC_BACKEND"] = "sqlite"

            try:
                import config
                import ipc

                importlib.reload(config)
                importlib.reload(ipc)

                session_id = f"marker_{int(time.time() * 1000)}"
                files = ipc.SessionFiles(session_id)
                marker = Path(files.marker_file)
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text(
                    '{"status":"waiting","session":"%s","pid":99999999}' % session_id,
                    encoding="utf-8",
                )

                self.assertFalse(files.is_waiting)
            finally:
                if old_runtime is None:
                    os.environ.pop("ACLI_IPC_RUNTIME_DIR", None)
                else:
                    os.environ["ACLI_IPC_RUNTIME_DIR"] = old_runtime
                if old_backend is None:
                    os.environ.pop("ACLI_IPC_BACKEND", None)
                else:
                    os.environ["ACLI_IPC_BACKEND"] = old_backend
                import config
                import ipc
                importlib.reload(config)
                importlib.reload(ipc)


class KillStatusRegressionTests(unittest.TestCase):
    def test_cli_kill_resolves_model_alias(self) -> None:
        with tempfile.TemporaryDirectory(prefix="acli_kill_alias_") as td:
            env = os.environ.copy()
            env["ACLI_SESSION_DIR"] = td
            env["ACLI_IPC_RUNTIME_DIR"] = td

            workspace = os.path.join(td, "ws")
            os.makedirs(workspace, exist_ok=True)

            sleeper = subprocess.Popen(["sleep", "30"])
            try:
                seed = (
                    "import json, os, time;"
                    "from session_manager import make_session_id;"
                    f"workspace={workspace!r};"
                    "model='sonnet-4.5';"
                    "sid=make_session_id(workspace, model);"
                    f"meta_dir={td!r};"
                    "st=int(open(f'/proc/{os.getpid()}/stat').read().split()[21]);"
                    # override with sleeper PID ticks
                    f"st=int(open('/proc/{sleeper.pid}/stat').read().split()[21]);"
                    "meta={"
                    "'session_id':sid,"
                    "'workspace':workspace,"
                    "'model':model,"
                    f"'pid':{sleeper.pid},"
                    "'process_start_ticks':st,"
                    "'created_at':time.time(),"
                    "'last_active':time.time(),"
                    "'round_count':1"
                    "};"
                    "open(os.path.join(meta_dir, f'{sid}.json'),'w').write(json.dumps(meta));"
                    "print(sid)"
                )
                seeded = subprocess.run(
                    ["python3", "-c", seed],
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=5,
                )
                self.assertEqual(seeded.returncode, 0, seeded.stderr)

                cmd = subprocess.run(
                    [
                        "python3",
                        "acli.py",
                        "--workspace",
                        workspace,
                        "--model",
                        "sonnet",
                        "kill",
                    ],
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=10,
                )
                self.assertEqual(cmd.returncode, 0, cmd.stderr)
                self.assertIn("Agent 已终止", cmd.stdout)
            finally:
                sleeper.terminate()
                try:
                    sleeper.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    sleeper.kill()

    def test_stop_and_kill_all_removes_session_meta(self) -> None:
        script = """
import os, tempfile, time, importlib
td = tempfile.mkdtemp(prefix='acli_meta_rm_')
os.environ['ACLI_SESSION_DIR'] = td
os.environ['ACLI_IPC_RUNTIME_DIR'] = td
os.environ['ACLI_IPC_BACKEND'] = 'sqlite'

import config, session_manager, agent_process
importlib.reload(config)
importlib.reload(agent_process)
importlib.reload(session_manager)

mgr = session_manager.SessionManager()
agent = agent_process.AgentProcess('acli_meta_rm', '/tmp', 'sonnet-4.5')
agent.pid = 0
agent.created_at = time.time()
agent.last_active = time.time()
agent.round_count = 1
mgr._sessions[agent.session_id] = agent
mgr._save_session_meta(agent)
meta = os.path.join(td, f'{agent.session_id}.json')
print('before', os.path.exists(meta))
mgr.stop_and_kill_all()
print('after', os.path.exists(meta))
"""
        result = subprocess.run(
            ["python3", "-c", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("before True", result.stdout)
        self.assertIn("after False", result.stdout)

    def test_list_sessions_excludes_dead_processes(self) -> None:
        script = """
import os, tempfile, time, importlib
td = tempfile.mkdtemp(prefix='acli_list_live_')
os.environ['ACLI_SESSION_DIR'] = td
os.environ['ACLI_IPC_RUNTIME_DIR'] = td
os.environ['ACLI_IPC_BACKEND'] = 'sqlite'

import config, session_manager, agent_process
importlib.reload(config)
importlib.reload(agent_process)
importlib.reload(session_manager)

mgr = session_manager.SessionManager()
agent = agent_process.AgentProcess('acli_dead', '/tmp', 'sonnet-4.5')
agent.pid = 0
agent.created_at = time.time()
agent.last_active = time.time()
agent.round_count = 1
mgr._sessions[agent.session_id] = agent
print('count', len(mgr.list_sessions()))
"""
        result = subprocess.run(
            ["python3", "-c", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("count 0", result.stdout)


class ReconnectReplayRegressionTests(unittest.TestCase):
    def test_read_events_ignores_replayed_wait_started_call_id(self) -> None:
        with tempfile.TemporaryDirectory(prefix="acli_reconnect_") as td:
            old_runtime = os.environ.get("ACLI_IPC_RUNTIME_DIR")
            old_backend = os.environ.get("ACLI_IPC_BACKEND")
            os.environ["ACLI_IPC_RUNTIME_DIR"] = td
            os.environ["ACLI_IPC_BACKEND"] = "sqlite"

            try:
                import config
                import ipc

                importlib.reload(config)
                importlib.reload(ipc)

                sid = "acli_reconnect_case"
                files = ipc.SessionFiles(sid)
                Path(files.output_file).parent.mkdir(parents=True, exist_ok=True)

                # 标记为 waiting，避免 read_events 在 wait 检测处长时间轮询
                Path(files.marker_file).write_text(
                    json.dumps({"session": sid, "pid": os.getpid()}),
                    encoding="utf-8",
                )

                wait_started = {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": "call_wait_1",
                    "tool_call": {
                        "shellToolCall": {
                            "args": {"command": "python3 /x/wait_input.py acli_reconnect_case 86400"}
                        }
                    },
                }

                with open(files.output_file, "w", encoding="utf-8") as f:
                    f.write(json.dumps(wait_started) + "\n")

                first = list(files.read_events(read_pos=0, timeout=1))
                self.assertTrue(first)
                read_pos = first[-1][1]

                assistant_ev = {
                    "type": "assistant",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": "AFTER_RECONNECT"}]},
                }

                # 模拟重连回放：同 call_id 的 wait_started 被重复写入
                with open(files.output_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(wait_started) + "\n")
                    f.write(json.dumps(assistant_ev) + "\n")

                second = list(files.read_events(read_pos=read_pos, timeout=1))
                event_types = [ev.get("type") for ev, _ in second]
                self.assertIn("assistant", event_types)
            finally:
                if old_runtime is None:
                    os.environ.pop("ACLI_IPC_RUNTIME_DIR", None)
                else:
                    os.environ["ACLI_IPC_RUNTIME_DIR"] = old_runtime
                if old_backend is None:
                    os.environ.pop("ACLI_IPC_BACKEND", None)
                else:
                    os.environ["ACLI_IPC_BACKEND"] = old_backend
                import config
                import ipc
                importlib.reload(config)
                importlib.reload(ipc)


class BusySessionPolicyRegressionTests(unittest.TestCase):
    def test_sqlite_busy_session_enqueues_without_ready_wait(self) -> None:
        script = """
import os, tempfile, time, importlib
td = tempfile.mkdtemp(prefix='acli_busy_sqlite_')
os.environ['ACLI_SESSION_DIR'] = td
os.environ['ACLI_IPC_RUNTIME_DIR'] = td
os.environ['ACLI_IPC_BACKEND'] = 'sqlite'

import config, session_manager, agent_process
importlib.reload(config)
importlib.reload(agent_process)
importlib.reload(session_manager)

mgr = session_manager.SessionManager()
sid = session_manager.make_session_id('/tmp', 'sonnet-4.5')
agent = agent_process.AgentProcess(sid, '/tmp', 'sonnet-4.5')

# 恢复态 agent：proc handle 不存在，但 PID 活着
agent.proc = None
agent.pid = os.getpid()
agent.process_start_ticks = int(open(f'/proc/{os.getpid()}/stat').read().split()[21])
agent.created_at = time.time()
agent.last_active = time.time()
agent.round_count = 1

flags = {'wait_called': False, 'kill_called': False, 'send_called': 0}

def fake_kill():
    flags['kill_called'] = True

def fake_send(_text):
    flags['send_called'] += 1

agent.kill = fake_kill
agent.send_input = fake_send
mgr._sessions[sid] = agent

out = mgr.get_or_create('/tmp', 'sonnet-4.5', 'hello')
print('same_agent', out is agent)
print('wait_called', flags['wait_called'])
print('kill_called', flags['kill_called'])
print('send_called', flags['send_called'])
"""
        result = subprocess.run(
            ["python3", "-c", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("same_agent True", result.stdout)
        self.assertIn("wait_called False", result.stdout)
        self.assertIn("kill_called False", result.stdout)
        self.assertIn("send_called 1", result.stdout)


class PromptReminderRegressionTests(unittest.TestCase):
    def test_send_input_appends_second_reminder(self) -> None:
        from agent_process import AgentProcess

        agent = AgentProcess("acli_reminder_case", "/tmp", "sonnet-4.5")
        captured = {"text": ""}

        def fake_send_input(text: str) -> None:
            captured["text"] = text

        agent.files.send_input = fake_send_input  # type: ignore[assignment]
        agent.send_input("HELLO")

        self.assertIn("ACLI_WORKFLOW_REMINDER", captured["text"])
        self.assertIn("wait_input.py", captured["text"])
        self.assertIn("acli_reminder_case", captured["text"])


class ResultWithoutWaitRegressionTests(unittest.TestCase):
    def test_read_events_emits_internal_event_when_result_has_no_wait_input(self) -> None:
        with tempfile.TemporaryDirectory(prefix="acli_result_no_wait_") as td:
            old_runtime = os.environ.get("ACLI_IPC_RUNTIME_DIR")
            old_backend = os.environ.get("ACLI_IPC_BACKEND")
            old_grace = os.environ.get("ACLI_WAIT_INPUT_AFTER_RESULT_GRACE_SECS")
            os.environ["ACLI_IPC_RUNTIME_DIR"] = td
            os.environ["ACLI_IPC_BACKEND"] = "sqlite"
            os.environ["ACLI_WAIT_INPUT_AFTER_RESULT_GRACE_SECS"] = "0.2"

            try:
                import config
                import ipc

                importlib.reload(config)
                importlib.reload(ipc)

                sid = "acli_result_no_wait_case"
                files = ipc.SessionFiles(sid)
                Path(files.output_file).parent.mkdir(parents=True, exist_ok=True)
                with open(files.output_file, "w", encoding="utf-8") as f:
                    f.write(json.dumps({"type": "result", "subtype": "success"}) + "\n")

                events = list(files.read_events(read_pos=0, timeout=1))
                self.assertTrue(events)
                types = [ev.get("type") for ev, _ in events]
                self.assertIn("result", types)
                self.assertIn("acli_internal", types)
                self.assertTrue(
                    any(
                        ev.get("type") == "acli_internal"
                        and ev.get("subtype") == "missing_wait_input_after_result"
                        for ev, _ in events
                    )
                )
            finally:
                if old_runtime is None:
                    os.environ.pop("ACLI_IPC_RUNTIME_DIR", None)
                else:
                    os.environ["ACLI_IPC_RUNTIME_DIR"] = old_runtime
                if old_backend is None:
                    os.environ.pop("ACLI_IPC_BACKEND", None)
                else:
                    os.environ["ACLI_IPC_BACKEND"] = old_backend
                if old_grace is None:
                    os.environ.pop("ACLI_WAIT_INPUT_AFTER_RESULT_GRACE_SECS", None)
                else:
                    os.environ["ACLI_WAIT_INPUT_AFTER_RESULT_GRACE_SECS"] = old_grace
                import config
                import ipc
                importlib.reload(config)
                importlib.reload(ipc)


class LoggingRegressionTests(unittest.TestCase):
    def test_acli_status_writes_log_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="acli_logs_") as td:
            env = os.environ.copy()
            env["ACLI_LOG_DIR"] = td
            env["ACLI_SESSION_DIR"] = td
            env["ACLI_IPC_RUNTIME_DIR"] = td

            result = subprocess.run(
                ["python3", "acli.py", "status"],
                capture_output=True,
                text=True,
                env=env,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            log_file = Path(td) / "acli.log"
            self.assertTrue(log_file.exists(), "acli status should write logs into file")
            self.assertGreater(log_file.stat().st_size, 0)
            content = log_file.read_text(encoding="utf-8", errors="replace")
            self.assertIn("subcommand status start", content)
            self.assertIn("subcommand status result", content)

    def test_wait_input_writes_log_file(self) -> None:
        session_id = f"log_{int(time.time() * 1000)}"
        with tempfile.TemporaryDirectory(prefix="acli_wait_logs_") as td:
            env = os.environ.copy()
            env["ACLI_LOG_DIR"] = td
            env["ACLI_IPC_RUNTIME_DIR"] = td
            enqueue = subprocess.run(
                [
                    "python3",
                    "-c",
                    (
                        "from ipc import SessionFiles;"
                        f"s=SessionFiles('{session_id}');"
                        "s.send_input('PING')"
                    ),
                ],
                capture_output=True,
                text=True,
                env=env,
                timeout=5,
            )
            self.assertEqual(enqueue.returncode, 0, enqueue.stderr)

            result = subprocess.run(
                ["python3", "wait_input.py", session_id, "1"],
                capture_output=True,
                text=True,
                env=env,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            wait_log = Path(td) / "acli.wait_input.log"
            self.assertTrue(wait_log.exists(), "wait_input should write logs into file")
            content = wait_log.read_text(encoding="utf-8", errors="replace")
            self.assertIn("等待脚本启动", content)
            self.assertIn("运行上下文", content)

    def test_wait_input_timeout_emits_poll_trace_logs(self) -> None:
        session_id = f"trace_{int(time.time() * 1000)}"
        with tempfile.TemporaryDirectory(prefix="acli_wait_trace_") as td:
            env = os.environ.copy()
            env["ACLI_LOG_DIR"] = td
            env["ACLI_IPC_RUNTIME_DIR"] = td
            env["ACLI_WAIT_INPUT_TRACE_EVERY"] = "1"

            result = subprocess.run(
                ["python3", "wait_input.py", session_id, "1"],
                capture_output=True,
                text=True,
                env=env,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("[SESSION_TIMEOUT]", result.stdout)

            wait_log = Path(td) / "acli.wait_input.log"
            self.assertTrue(wait_log.exists(), "wait_input should write logs into file")
            content = wait_log.read_text(encoding="utf-8", errors="replace")
            self.assertIn("轮询状态", content)
            self.assertIn("等待超时", content)


if __name__ == "__main__":
    unittest.main()
