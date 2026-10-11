"""离线验证：使用临时目录，不连接服务器、不修改用户的 ~/.ssh。"""

import contextlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import setup_ssh


class SetupSSHTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.target = setup_ssh.Target("connect.westx.seetacloud.com", "root", 29925)

    def test_command_formats_and_names(self):
        for command in (
            r"ssh -p 29925 root\@connect.westx.seetacloud.com",
            "ssh -p29925 -l root connect.westx.seetacloud.com",
            "ssh root@connect.westx.seetacloud.com -p 29925",
        ):
            self.assertEqual(setup_ssh.parse_ssh_command(command), self.target)
        self.assertEqual(self.target.default_name, "westx-29925")
        self.assertEqual(setup_ssh.parse_ssh_command("ssh dev@192.0.2.1").default_name, "192.0.2.1-22")
        self.assertEqual(setup_ssh.parse_ssh_command("ssh -p 2222 dev@[::1]").host, "::1")
        self.assertEqual(setup_ssh.validate_name("my-gpu_01"), "my-gpu_01")

    def test_rejects_command_execution_and_invalid_input(self):
        for command in (
            "ssh -o ProxyCommand=evil root@example.com",
            "ssh root@example.com; touch /tmp/should-not-exist",
            "ssh root@$(hostname)", "ssh root@example.com reboot",
            "ssh -p 65536 root@example.com", "ssh -p -1 root@example.com",
            "ssh example.com", "ssh -p", "ssh -l other root@example.com",
        ):
            with self.subTest(command=command), self.assertRaises(ValueError):
                setup_ssh.parse_ssh_command(command)
        for name in ("../config", "*", "-bad", "name\nHost *", "a" * 65):
            with self.subTest(name=name), self.assertRaises(ValueError):
                setup_ssh.validate_name(name)

    def test_dry_run_does_not_setup(self):
        output = io.StringIO()
        with patch.object(setup_ssh, "setup") as install, contextlib.redirect_stdout(output):
            status = setup_ssh.main(["ssh -p 29925 root@connect.westx.seetacloud.com", "--dry-run"])
        self.assertEqual(status, 0)
        install.assert_not_called()
        self.assertIn("ssh westx-29925", output.getvalue())

    def test_config_repeat_update_backup_and_existing_settings(self):
        original = "# Existing settings\nHost *\n    Port 2222\n    User old\nHost old-box\n    HostName example.org\n"
        config = self.directory / "config"
        config.write_text(original)
        key_path = self.directory / "key with spaces%"
        updated = setup_ssh.config_text(original, "westx-29925", self.target, key_path)
        self.assertTrue(updated.endswith(original))
        self.assertEqual(setup_ssh.config_text(updated, "westx-29925", self.target, key_path), updated)
        backup = setup_ssh.save_config(config, original, updated)
        self.assertEqual(backup.read_text(), original)
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        # 由真实 OpenSSH 解析配置，验证泛匹配不会抢先覆盖别名的主机、用户、端口。
        result = subprocess.run(["ssh", "-G", "-F", str(config), "westx-29925"], capture_output=True, text=True, check=True)
        self.assertIn("hostname connect.westx.seetacloud.com\n", result.stdout)
        self.assertIn("port 29925\n", result.stdout)
        self.assertIn("user root\n", result.stdout)
        old = subprocess.run(["ssh", "-G", "-F", str(config), "old-box"], capture_output=True, text=True, check=True)
        self.assertIn("hostname example.org\n", old.stdout)
        self.assertIn("port 2222\n", old.stdout)
        self.assertIsNone(setup_ssh.save_config(config, updated, updated))

    def test_conflict_and_concurrent_edit_preserve_config(self):
        with self.assertRaises(ValueError):
            setup_ssh.config_text("Host gpu\n HostName old\n", "gpu", self.target, self.directory / "key")
        with self.assertRaises(ValueError):
            setup_ssh.config_text("# BEGIN setup_ssh gpu\n", "gpu", self.target, self.directory / "key")
        config = self.directory / "config"
        config.write_text("concurrent edit\n")
        with self.assertRaises(ValueError):
            setup_ssh.save_config(config, "old content\n", "new content\n")
        self.assertEqual(config.read_text(), "concurrent edit\n")

    def test_real_key_reuse_and_remote_authorized_keys_preservation(self):
        key = self.directory / "id_ed25519_gpu"
        public_key = setup_ssh.ensure_key(key, "gpu")
        private_bytes = key.read_bytes()
        Path(str(key) + ".pub").write_text("stale public key\n")
        self.assertEqual(setup_ssh.ensure_key(key, "gpu"), public_key)
        self.assertEqual(key.read_bytes(), private_bytes)
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        self.assertEqual(Path(str(key) + ".pub").read_text(), public_key + "\n")
        remote = self.directory / "remote"
        auth = remote / ".ssh" / "authorized_keys"
        auth.parent.mkdir(parents=True)
        auth.write_text("existing-key-without-final-newline")
        for _ in range(2):
            subprocess.run(["sh", "-s"], input=setup_ssh.install_script(public_key), text=True,
                           env={**os.environ, "HOME": str(remote)}, check=True)
        self.assertEqual(auth.read_text().splitlines(), ["existing-key-without-final-newline", public_key])
        self.assertEqual(auth.stat().st_mode & 0o777, 0o600)
        self.assertEqual(auth.parent.stat().st_mode & 0o777, 0o700)

    def test_failed_verification_does_not_save_config(self):
        ssh_dir = self.directory / ".ssh"
        ssh_dir.mkdir()
        config = ssh_dir / "config"
        original = "Host existing\n    HostName existing.example\n"
        config.write_text(original)
        calls = []
        real_run = subprocess.run

        def run(command, **kwargs):
            if command[0] == "ssh":
                calls.append(command)
                if command[-1] == "true":
                    raise subprocess.CalledProcessError(255, command)
                return subprocess.CompletedProcess(command, 0)
            return real_run(command, **kwargs)

        with patch.object(setup_ssh.subprocess, "run", side_effect=run), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(subprocess.CalledProcessError):
                setup_ssh.setup(self.target, "gpu", ssh_dir)
        self.assertEqual(len(calls), 2)
        self.assertIn("BatchMode=yes", calls[-1])
        self.assertIn("PreferredAuthentications=publickey", calls[-1])
        self.assertEqual(config.read_text(), original)
        self.assertFalse(list(ssh_dir.glob("config.backup-*")))


if __name__ == "__main__":
    unittest.main()
