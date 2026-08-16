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
            runner = autoprompt.write_runner(job)
            script = runner.read_text(encoding="utf-8")
            prompt_snapshot = autoprompt.prompt_path_for(job.name)

        self.assertIn('TARGET=research:1.2', script)
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

    def test_idle_check_requires_static_empty_agent_input(self) -> None:
        with (
            patch.object(autoprompt, "tmux_capture", side_effect=["❯  \n", "❯  \n"]),
            patch.object(autoprompt.time, "sleep"),
        ):
            self.assertTrue(autoprompt.tmux_target_is_idle("%12", samples=2))

        with (
            patch.object(autoprompt, "tmux_capture", side_effect=["❯\n", "working\n"]),
            patch.object(autoprompt.time, "sleep"),
        ):
            self.assertFalse(autoprompt.tmux_target_is_idle("%12", samples=2))

        with (
            patch.object(autoprompt, "tmux_capture", return_value="❯ 下書き\n"),
            patch.object(autoprompt.time, "sleep"),
        ):
            self.assertFalse(autoprompt.tmux_target_is_idle("%12", samples=2))

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
            patch.object(autoprompt, "tmux_send_prompt") as send_prompt,
            self.assertRaises(SystemExit),
        ):
            autoprompt.cmd_send(args)
        send_prompt.assert_not_called()

        args.force = True
        with (
            patch.object(autoprompt, "tmux_resolve_target", return_value="%7"),
            patch.object(autoprompt, "tmux_send_prompt") as send_prompt,
        ):
            autoprompt.cmd_send(args)
        send_prompt.assert_called_once_with("%7", "続けて")


if __name__ == "__main__":
    unittest.main()
