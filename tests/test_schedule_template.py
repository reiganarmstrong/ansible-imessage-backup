from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest


TEMPLATE = (
    Path(__file__).parents[1]
    / "templates"
    / "run-scheduled-backup.zsh.j2"
)


class ScheduleTemplateTests(unittest.TestCase):
    def render(
        self,
        home: Path,
        playbook_directory: Path,
        ansible_playbook: Path,
    ) -> str:
        content = TEMPLATE.read_text(encoding="utf-8")
        replacements = {
            "{{ ansible_facts['env']['HOME'] }}": str(home),
            "{{ imessage_schedule_path }}": "/usr/bin:/bin",
            "{{ playbook_dir }}": str(playbook_directory),
            "{{ imessage_schedule_log_retention_days | int }}": "90",
            "{{ imessage_ansible_playbook_lookup.stdout | trim }}": str(
                ansible_playbook
            ),
        }
        for source, destination in replacements.items():
            content = content.replace(source, destination)
        self.assertNotIn("{{", content)
        return content

    def test_wrapper_has_lock_status_and_log_retention(self) -> None:
        content = TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("umask 077", content)
        self.assertIn('backup_lock_dir=', content)
        self.assertIn('outcome" == "succeeded"', content)
        self.assertIn('"skipped_already_running"', content)
        self.assertIn('"running" "null" "last-run.json"', content)
        self.assertIn('"last-skipped.json"', content)
        self.assertIn('"interrupted"', content)
        self.assertIn("imessage_schedule_log_retention_days", content)
        self.assertNotIn('exec "{{ imessage_ansible_playbook_lookup', content)

    @unittest.skipUnless(shutil.which("zsh"), "zsh is not installed")
    def test_concurrent_wrapper_runs_only_one_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / "home"
            repository = root / "repository"
            home.mkdir()
            repository.mkdir()
            calls = root / "calls.txt"
            fake_ansible = root / "fake-ansible-playbook"
            fake_ansible.write_text(
                "#!/bin/zsh\n"
                "/usr/bin/printf 'called\\n' >> \"$FAKE_CALLS\"\n"
                "/bin/sleep 2\n",
                encoding="utf-8",
            )
            fake_ansible.chmod(0o700)
            wrapper = root / "wrapper.zsh"
            wrapper.write_text(
                self.render(home, repository, fake_ansible),
                encoding="utf-8",
            )
            wrapper.chmod(0o700)
            environment = os.environ.copy()
            environment["FAKE_CALLS"] = str(calls)

            first = subprocess.Popen(
                [str(wrapper)],
                env=environment,
            )
            lock_pid = (
                home
                / "Library"
                / "Caches"
                / "ansible-imessage-backup"
                / "run.lock"
                / "pid"
            )
            for _ in range(40):
                if lock_pid.is_file():
                    break
                time.sleep(0.05)
            self.assertTrue(lock_pid.is_file())
            last_run_path = (
                home
                / "Library"
                / "Application Support"
                / "ansible-imessage-backup"
                / "last-run.json"
            )
            for _ in range(40):
                if last_run_path.is_file():
                    break
                time.sleep(0.05)
            running = json.loads(
                last_run_path.read_text(encoding="utf-8")
            )
            self.assertEqual(running["outcome"], "running")
            self.assertIsNone(running["exit_code"])
            self.assertIsNone(running["ended_at"])
            second = subprocess.run(
                [str(wrapper)],
                env=environment,
                check=False,
            )
            self.assertEqual(second.returncode, 0)
            still_running = json.loads(
                last_run_path.read_text(encoding="utf-8")
            )
            self.assertEqual(still_running["outcome"], "running")
            skipped = json.loads(
                (
                    home
                    / "Library"
                    / "Application Support"
                    / "ansible-imessage-backup"
                    / "last-skipped.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                skipped["outcome"],
                "skipped_already_running",
            )
            self.assertEqual(first.wait(timeout=10), 0)
            self.assertEqual(
                calls.read_text(encoding="utf-8").splitlines(),
                ["called"],
            )
            last_run = json.loads(last_run_path.read_text(encoding="utf-8"))
            self.assertEqual(last_run["outcome"], "succeeded")
