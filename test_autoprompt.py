import argparse
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
        self.assertNotIn("tmux new-session", script)
        self.assertNotIn("command -v claude", script)
        self.assertEqual(prompt_snapshot.read_text(encoding="utf-8"), "1行目\\\n2行目")

    def test_cmd_send_requires_idle_target_unless_forced(self) -> None:
        args = argparse.Namespace(
            target="research:1.2",
            prompt="続けて",
            prompt_file=None,
            force=False,
        )
        with (
            patch.object(autoprompt, "tmux_target_exists", return_value=True),
            patch.object(autoprompt, "tmux_target_is_idle", return_value=False),
            patch.object(autoprompt, "tmux_send_prompt") as send_prompt,
            self.assertRaises(SystemExit),
        ):
            autoprompt.cmd_send(args)
        send_prompt.assert_not_called()

        args.force = True
        with (
            patch.object(autoprompt, "tmux_target_exists", return_value=True),
            patch.object(autoprompt, "tmux_send_prompt") as send_prompt,
        ):
            autoprompt.cmd_send(args)
        send_prompt.assert_called_once_with("research:1.2", "続けて")


if __name__ == "__main__":
    unittest.main()
