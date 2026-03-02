import os
import unittest


class GatewayHelperTests(unittest.TestCase):
    def test_resolve_workspace_prefers_header(self) -> None:
        from gateway_helpers import resolve_workspace_path

        default_workspace = "/tmp/default_ws"
        headers = {"x-workspace-path": "/tmp/from_header"}
        metadata = {"cwd": "/tmp/from_metadata"}

        got = resolve_workspace_path(
            headers=headers,
            metadata=metadata,
            default_workspace=default_workspace,
        )
        self.assertEqual(got, os.path.abspath("/tmp/from_header"))

    def test_resolve_workspace_from_metadata(self) -> None:
        from gateway_helpers import resolve_workspace_path

        got = resolve_workspace_path(
            headers={},
            metadata={"cwd": "/tmp/from_metadata"},
            default_workspace="/tmp/default_ws",
        )
        self.assertEqual(got, os.path.abspath("/tmp/from_metadata"))

    def test_resolve_workspace_fallback_default(self) -> None:
        from gateway_helpers import resolve_workspace_path

        got = resolve_workspace_path(
            headers={},
            metadata={},
            default_workspace="/tmp/default_ws",
        )
        self.assertEqual(got, os.path.abspath("/tmp/default_ws"))

    def test_extract_prompt_last_only_uses_last_user(self) -> None:
        from gateway_helpers import extract_prompt_last_openai

        messages = [
            {"role": "system", "content": "S"},
            {"role": "user", "content": "U1"},
            {"role": "assistant", "content": "A1"},
            {"role": "user", "content": "U2"},
        ]

        got = extract_prompt_last_openai(messages)
        self.assertEqual(got, "U2")

    def test_build_session_id_changes_with_workspace(self) -> None:
        from gateway_helpers import build_session_id

        sid_a = build_session_id("/tmp/ws_a", "sonnet-4.5")
        sid_b = build_session_id("/tmp/ws_b", "sonnet-4.5")
        self.assertNotEqual(sid_a, sid_b)


if __name__ == "__main__":
    unittest.main()
