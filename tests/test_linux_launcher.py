"""Validate launcher configuration without starting Docker or hardware services."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipIf(
    os.name == "nt", "Linux shell behavior is checked on the Linux CI runners"
)
class LinuxLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "scripts").mkdir()
        (self.root / "bin").mkdir()
        repo = Path(__file__).resolve().parent.parent
        shutil.copy(repo / "scripts" / "linux-start.sh", self.root / "scripts")
        shutil.copy(repo / ".env.example", self.root / ".env.example")
        docker = self.root / "bin" / "docker"
        docker.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$*" >> "$VDS_TEST_DOCKER_LOG"\n',
            encoding="utf-8",
        )
        docker.chmod(0o700)
        self.environment = dict(
            os.environ,
            PATH=str(self.root / "bin") + os.pathsep + os.environ["PATH"],
            VDS_TEST_DOCKER_LOG=str(self.root / "calls.log"),
            VDS_ADMIN_PASSWORD="",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def launch(self, mode):
        return subprocess.run(
            ["bash", str(self.root / "scripts" / "linux-start.sh"), mode],
            cwd=self.root,
            env=self.environment,
            text=True,
            capture_output=True,
            check=True,
        )

    def test_first_run_creates_private_password_without_printing_it(self):
        result = self.launch("--build")
        config = (self.root / ".env").read_text()
        password = next(
            line.split("=", 1)[1]
            for line in config.splitlines()
            if line.startswith("VDS_ADMIN_PASSWORD=")
        )
        self.assertEqual(len(password), 48)
        self.assertNotIn(password, result.stdout + result.stderr)
        self.assertEqual((self.root / ".env").stat().st_mode & 0o777, 0o600)
        self.assertIn("up -d --build --wait", (self.root / "calls.log").read_text())

    def test_image_mode_preserves_existing_configuration(self):
        config = "VDS_ADMIN_PASSWORD=keep-this-local-test-value\nVDS_PORT=8060\n"
        (self.root / ".env").write_text(config)
        self.launch("--image")
        self.assertEqual((self.root / ".env").read_text(), config)
        calls = (self.root / "calls.log").read_text()
        self.assertIn("compose -f compose.image.yaml pull", calls)
        self.assertNotIn("--build", calls)


if __name__ == "__main__":
    unittest.main()
