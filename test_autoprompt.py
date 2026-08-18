import argparse
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import autoprompt


class ExistingTmuxJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)

    def write_job(self, body: str) -> Path:
        job_path = self.root / "existing.toml"
        job_path.write_text(body, encoding="utf-8")
        return job_path

    def test_existing_tmux_job_does_not_require_cwd(self) -> None:
        job = autoprompt.load_job(
            self.write_job(
                'name = "existing"\n'
                'tmux_target = "research:1.2"\n'
                'prompt = "続きを実行してください"\n'
            )
        )

        self.assertIsNone(job.cwd)
        self.assertEqual(job.tmux_target, "research:1.2")

    def test_existing_tmux_job_rejects_watchdog_configuration(self) -> None:
        with self.assertRaises(SystemExit):
            autoprompt.load_job(
                self.write_job(
                    'name = "existing"\n'
                    'tmux_target = "research"\n'
                    'prompt = "続きを実行してください"\n'
                    'on_rate_limit = "codex"\n'
                )
            )

    def test_runner_targets_existing_pane_without_starting_claude(self) -> None:
        job = autoprompt.Job(
            {
                "name": "existing",
                "cwd": None,
                "tmux_target": "research:1.2",
                "prompt": "1行目\n2行目",
            },
            self.root / "existing.toml",
        )
        state_directory = self.root / "state"
        log_directory = self.root / "logs"
        state_directory.mkdir()
        log_directory.mkdir()

        with (
            patch.object(autoprompt, "STATE_DIR", state_directory),
            patch.object(autoprompt, "LOG_DIR", log_directory),
        ):
            runner = autoprompt.write_runner(
                job,
                tmux_identity={
                    "pane_id": "%12",
                    "pane_pid": "12345",
                    "pane_start_command": "claude",
                    "pane_current_command": "2.1.233",
                    "session_id": "$3",
                    "window_id": "@8",
                },
            )
            script = runner.read_text(encoding="utf-8")
            prompt_snapshot = autoprompt.prompt_path_for(job.name)

        self.assertIn('TARGET=%12', script)
        self.assertIn("display-message", script)
        self.assertIn("capture-pane", script)
        self.assertIn("paste-buffer", script)
        self.assertIn("printf '%s\\n' \"$AFTER\"", script)
        self.assertIn(
            "printf '{\"name\":\"existing\",\"status\":\"sent\"",
            script,
        )
        self.assertNotIn("tmux new-session", script)
        self.assertNotIn("command -v claude", script)
        self.assertNotIn("1行目", script)
        self.assertEqual(prompt_snapshot.read_text(encoding="utf-8"), "1行目\\\n2行目")
        self.assertEqual(stat.S_IMODE(prompt_snapshot.stat().st_mode), 0o600)

    def test_tmux_target_is_resolved_to_stable_pane_id(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="%12\n", stderr="")
        with patch.object(autoprompt.subprocess, "run", return_value=completed) as run:
            pane_id = autoprompt.tmux_resolve_target("research:1.2")

        self.assertEqual(pane_id, "%12")
        run.assert_called_once_with(
            ["tmux", "display-message", "-p", "-t", "research:1.2", "#{pane_id}"],
            capture_output=True,
            text=True,
        )

    def test_tmux_identity_allows_empty_start_command_for_manual_session(self) -> None:
        outputs = ["%12\n", "12345\n", "\n", "2.1.233\n", "$3\n", "@8\n"]
        completed = [
            subprocess.CompletedProcess([], 0, stdout=output, stderr="")
            for output in outputs
        ]
        with patch.object(autoprompt.subprocess, "run", side_effect=completed):
            identity = autoprompt.tmux_target_identity("research:1.2")

        self.assertIsNotNone(identity)
        self.assertEqual(identity["pane_start_command"], "")

    def test_idle_check_requires_static_empty_agent_input(self) -> None:
        with (
            patch.object(autoprompt, "tmux_capture", side_effect=["❯  \n", "❯  \n"]),
            patch.object(autoprompt.time, "sleep") as sleep,
        ):
            self.assertTrue(autoprompt.tmux_target_is_idle("%12", samples=2))
        self.assertEqual(sleep.call_count, 1)

        with (
            patch.object(autoprompt, "tmux_capture", side_effect=["❯\n", "working\n"]),
            patch.object(autoprompt.time, "sleep"),
        ):
            self.assertFalse(autoprompt.tmux_target_is_idle("%12", samples=2))

    def test_idle_check_accepts_claude_non_breaking_space_prompt(self) -> None:
        claude_empty_prompt = "❯\u00a0\n"
        with (
            patch.object(autoprompt, "tmux_capture", return_value=claude_empty_prompt),
            patch.object(autoprompt.time, "sleep"),
        ):
            self.assertTrue(autoprompt.tmux_target_is_idle("%12", samples=2))

        with (
            patch.object(autoprompt, "tmux_capture", return_value="❯ 下書き\n"),
            patch.object(autoprompt.time, "sleep"),
        ):
            self.assertFalse(autoprompt.tmux_target_is_idle("%12", samples=2))

        historical_prompt = "❯\n確認ダイアログ"
        with (
            patch.object(autoprompt, "tmux_capture", return_value=historical_prompt),
            patch.object(autoprompt.time, "sleep"),
        ):
            self.assertFalse(autoprompt.tmux_target_is_idle("%12", samples=2))

    def test_runner_verifies_original_pane_process_identity(self) -> None:
        job = autoprompt.Job(
            {
                "name": "identity",
                "cwd": None,
                "tmux_target": "research:1.2",
                "prompt": "続けて",
            },
            self.root / "identity.toml",
        )
        state_directory = self.root / "identity-state"
        log_directory = self.root / "identity-logs"
        state_directory.mkdir()
        log_directory.mkdir()
        identity = {
            "pane_id": "%12",
            "pane_pid": "12345",
            "pane_start_command": "claude",
            "pane_current_command": "2.1.233",
            "session_id": "$3",
            "window_id": "@8",
        }

        with (
            patch.object(autoprompt, "STATE_DIR", state_directory),
            patch.object(autoprompt, "LOG_DIR", log_directory),
        ):
            runner = autoprompt.write_runner(job, tmux_identity=identity)
            script = runner.read_text(encoding="utf-8")

        self.assertIn("#{pane_pid}", script)
        self.assertIn("#{pane_start_command}", script)
        self.assertIn("#{pane_current_command}", script)
        self.assertIn("#{session_id}", script)
        self.assertIn("#{window_id}", script)
        self.assertIn("%12|12345|claude|2.1.233|$3|@8", script)
        self.assertIn('rm -f "$PROMPT"', script)
        self.assertIn("autoprompt-identity-$$", script)

    def test_new_session_runner_does_not_embed_prompt_in_executable_script(self) -> None:
        secret_prompt = "TOP_SECRET_PROMPT"
        job = autoprompt.Job(
            {
                "name": "new-session",
                "cwd": self.root,
                "prompt": secret_prompt,
            },
            self.root / "new-session.toml",
        )
        state_directory = self.root / "new-state"
        log_directory = self.root / "new-logs"
        state_directory.mkdir()
        log_directory.mkdir()

        with (
            patch.object(autoprompt, "STATE_DIR", state_directory),
            patch.object(autoprompt, "LOG_DIR", log_directory),
        ):
            runner = autoprompt.write_runner(job)
            script = runner.read_text(encoding="utf-8")
            prompt_snapshot = autoprompt.prompt_path_for(job.name)

        self.assertNotIn(secret_prompt, script)
        self.assertEqual(prompt_snapshot.read_text(encoding="utf-8"), secret_prompt)
        self.assertEqual(stat.S_IMODE(prompt_snapshot.stat().st_mode), 0o600)

    def test_multiline_prompt_uses_one_unique_tmux_buffer(self) -> None:
        loaded_prompt = []

        def capture_load_buffer(command: list[str], **_: object) -> subprocess.CompletedProcess:
            if command[1] == "load-buffer":
                loaded_prompt.append(Path(command[-1]).read_text(encoding="utf-8"))
            return subprocess.CompletedProcess(command, 0)

        with (
            patch.object(autoprompt.subprocess, "run", side_effect=capture_load_buffer) as run,
            patch.object(autoprompt.time, "sleep"),
        ):
            autoprompt.tmux_send_prompt("%12", "1行目\n2行目")

        commands = [call.args[0] for call in run.call_args_list]
        load_command = next(command for command in commands if command[1] == "load-buffer")
        paste_command = next(command for command in commands if command[1] == "paste-buffer")
        self.assertEqual(loaded_prompt, ["1行目\\\n2行目"])
        self.assertTrue(load_command[3].startswith("autoprompt-"))
        self.assertEqual(load_command[3], paste_command[4])
        self.assertEqual(commands[-1], ["tmux", "send-keys", "-t", "%12", "Enter"])

    def test_cmd_send_requires_idle_target_unless_forced(self) -> None:
        args = argparse.Namespace(
            target="research:1.2",
            prompt="続けて",
            prompt_file=None,
            force=False,
        )
        with (
            patch.object(autoprompt, "tmux_resolve_target", return_value="%7"),
            patch.object(autoprompt, "tmux_target_is_idle", return_value=False),
            patch.object(autoprompt, "tmux_pane_current_command", return_value="2.1.233"),
            patch.object(autoprompt, "tmux_send_prompt") as send_prompt,
            self.assertRaises(SystemExit),
        ):
            autoprompt.cmd_send(args)
        send_prompt.assert_not_called()

        args.force = True
        with (
            patch.object(autoprompt, "tmux_resolve_target", return_value="%7"),
            patch.object(autoprompt, "tmux_pane_current_command", return_value="zsh"),
            patch.object(autoprompt, "tmux_send_prompt") as send_prompt,
        ):
            autoprompt.cmd_send(args)
        send_prompt.assert_called_once_with("%7", "続けて")

        args.force = False
        with (
            patch.object(autoprompt, "tmux_resolve_target", return_value="%7"),
            patch.object(autoprompt, "tmux_pane_current_command", return_value="zsh"),
            patch.object(autoprompt, "tmux_target_is_idle", return_value=True),
            patch.object(autoprompt, "tmux_send_prompt") as send_prompt,
            self.assertRaises(SystemExit),
        ):
            autoprompt.cmd_send(args)
        send_prompt.assert_not_called()

    def test_codex_handoff_never_embeds_captured_screen_in_shell_command(self) -> None:
        captured_screen = "malicious $(touch /tmp/should-not-run) ' \"\n"
        safe_runner = self.root / "handoff.runner.sh"
        with (
            patch.object(autoprompt, "tmux_capture", return_value=captured_screen),
            patch.object(autoprompt, "tmux_send_prompt") as send_prompt,
            patch.object(
                autoprompt,
                "tmux_pane_current_command",
                side_effect=["zsh", "codex"],
            ),
            patch.object(
                autoprompt,
                "_write_codex_handoff_runner",
                return_value=safe_runner,
            ),
            patch.object(autoprompt.subprocess, "run") as run,
            patch.object(autoprompt.time, "sleep"),
            patch.object(autoprompt, "watchdog_log"),
        ):
            result = autoprompt.handoff_to_codex("job", "%12")

        self.assertTrue(result)
        send_prompt.assert_called_once_with("%12", "/exit")
        literal_command = run.call_args_list[0].args[0][-1]
        self.assertEqual(literal_command, f"exec {safe_runner}")
        self.assertNotIn(captured_screen, literal_command)

    def test_codex_handoff_reports_failure_when_codex_does_not_start(self) -> None:
        safe_runner = self.root / "handoff.runner.sh"
        safe_runner.write_text("runner", encoding="utf-8")
        prompt_path = self.root / "job.handoff.prompt.txt"
        prompt_path.write_text("prompt", encoding="utf-8")
        with (
            patch.object(autoprompt, "tmux_capture", return_value="screen"),
            patch.object(autoprompt, "tmux_send_prompt"),
            patch.object(
                autoprompt,
                "tmux_pane_current_command",
                side_effect=["zsh", *("zsh" for _ in range(10))],
            ),
            patch.object(
                autoprompt,
                "_write_codex_handoff_runner",
                return_value=safe_runner,
            ),
            patch.object(autoprompt.subprocess, "run"),
            patch.object(autoprompt.time, "sleep"),
            patch.object(autoprompt, "STATE_DIR", self.root),
            patch.object(autoprompt, "watchdog_log"),
        ):
            result = autoprompt.handoff_to_codex("job", "%12")

        self.assertFalse(result)
        self.assertFalse(safe_runner.exists())
        self.assertFalse(prompt_path.exists())

    def test_codex_handoff_stops_if_claude_does_not_exit(self) -> None:
        with (
            patch.object(autoprompt, "tmux_capture", return_value="screen"),
            patch.object(autoprompt, "tmux_send_prompt"),
            patch.object(autoprompt, "tmux_pane_current_command", return_value="claude"),
            patch.object(autoprompt.subprocess, "run") as run,
            patch.object(autoprompt.time, "sleep"),
            patch.object(autoprompt, "watchdog_log"),
        ):
            result = autoprompt.handoff_to_codex("job", "%12")

        self.assertFalse(result)
        run.assert_not_called()

    def test_codex_handoff_runner_keeps_screen_content_out_of_script(self) -> None:
        prompt = "screen content $(dangerous) ' \""
        state_directory = self.root / "handoff-state"
        state_directory.mkdir()
        with patch.object(autoprompt, "STATE_DIR", state_directory):
            runner = autoprompt._write_codex_handoff_runner("job", prompt)
            prompt_path = state_directory / "job.handoff.prompt.txt"

        self.assertNotIn(prompt, runner.read_text(encoding="utf-8"))
        self.assertEqual(prompt_path.read_text(encoding="utf-8"), prompt)
        self.assertEqual(stat.S_IMODE(prompt_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(runner.stat().st_mode), 0o700)


if __name__ == "__main__":
    unittest.main()
