"""One-line installer: install, update and checksum refusal, with stand-ins for Docker (no network, no Docker)."""

import hashlib
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


@unittest.skipIf(os.name == "nt", "Linux shell behavior is checked on the Linux CI runners")
class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "bin").mkdir()
        release = self.root / "release"
        release.mkdir()
        bundle = release / "wilcoxon-vds-linux.tar.gz"
        with tarfile.open(bundle, "w:gz") as archive:  # a release bundle, as the release workflow builds it
            for path in ("scripts/linux-start.sh", "scripts/update.sh", "scripts/install.sh", ".env.example",
                         "docker/compose.yaml", "docker/compose.image.yaml"):
                archive.add(REPO / path, arcname=f"wilcoxon-vds/{path}")
        digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
        (release / "SHA256SUMS").write_text(f"{digest}  ./wilcoxon-vds-linux.tar.gz\n")
        docker = self.root / "bin" / "docker"
        docker.write_text('#!/bin/sh\nprintf "%s\n" "$*" >> "$VDS_TEST_DOCKER_LOG"\n')
        docker.chmod(0o700)
        self.target = self.root / "home" / "vds"
        self.env = dict(os.environ, PATH=f"{self.root / 'bin'}{os.pathsep}{os.environ['PATH']}",
                        VDS_TEST_DOCKER_LOG=str(self.root / "calls.log"), VDS_RELEASE_BASE=release.as_uri(),
                        VDS_INSTALL_DIR=str(self.target), VDS_ADMIN_PASSWORD="")
        self.release = release

    def tearDown(self):
        self.temporary.cleanup()

    def install(self, *args, check=True):
        return subprocess.run(["bash", str(REPO / "scripts" / "install.sh"), "--no-iriv", *args], env=self.env,
                              text=True, capture_output=True, check=check)

    def test_installs_starts_and_updates_keeping_the_password(self):
        result = self.install()
        self.assertIn("VDS-to-Dashboard is running", result.stdout)
        self.assertIn("compose.image.yaml up -d", (self.root / "calls.log").read_text())
        config = (self.target / ".env").read_text()
        password = next(line for line in config.splitlines() if line.startswith("VDS_ADMIN_PASSWORD="))
        self.assertNotIn(password.split("=", 1)[1], result.stdout + result.stderr)
        again = self.install()
        self.assertIn("Updating the existing install", again.stdout)
        self.assertIn(password, (self.target / ".env").read_text())

    def test_refuses_a_download_with_a_wrong_checksum(self):
        with open(self.release / "wilcoxon-vds-linux.tar.gz", "ab") as stream:
            stream.write(b"tampered")
        result = self.install(check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checksum does not match", result.stderr)
        self.assertFalse((self.target / ".env").exists())


if __name__ == "__main__":
    unittest.main()
