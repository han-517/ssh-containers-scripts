"""离线测试 Bash 版：真实生成密钥/执行远端脚本，仅用替身隔离 SSH 网络连接。"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import setup_ssh


ROOT = Path(__file__).resolve().parent


class BashSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.local = self.root / "local user"
        self.remote = self.root / "remote"
        self.local.mkdir()
        self.remote.mkdir()
        binary = self.root / "bin"
        binary.mkdir()
        ssh = binary / "ssh"
        ssh.write_text(f"#!{sys.executable}\n" + r'''
import json, os, pathlib, subprocess, sys
args = sys.argv[1:]
with open(os.environ['CALL_LOG'], 'a') as output:
    output.write(json.dumps(args) + '\n')
if args[-1] == 'sh -s':
    script = sys.stdin.read()
    result = subprocess.run(['sh', '-s'], input=script, text=True,
                            env={**os.environ, 'HOME': os.environ['REMOTE_HOME']})
    if os.environ.get('MUTATE_CONFIG'):
        pathlib.Path(os.environ['HOME'], '.ssh', 'config').write_text('concurrent edit\n')
    sys.exit(result.returncode)
if os.environ.get('FAIL_VERIFY'):
    sys.exit(255)
if os.environ.get('FAIL_ALIAS') and args[args.index('-F') + 1] != '/dev/null':
    sys.exit(255)
sys.exit(0)
''')
        ssh.chmod(0o755)
        self.env = {**os.environ, "HOME": str(self.local), "REMOTE_HOME": str(self.remote),
                    "CALL_LOG": str(self.root / "calls"), "PATH": str(binary) + os.pathsep + os.environ["PATH"]}

    def run_script(self, *args, env=None, input_text=None):
        return subprocess.run(["bash", str(ROOT / "setup_ssh.sh"), *args], env=env or self.env,
                              input=input_text, capture_output=True, text=True)

    def test_dry_runs_and_bad_commands_do_not_touch_ssh_directory(self):
        for command in (r"ssh -p 29925 root\@connect.westx.seetacloud.com",
                        "ssh -p29925 -l root connect.westx.seetacloud.com"):
            result = self.run_script(command, "--dry-run")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ssh westx-29925", result.stdout)
        for command in ("ssh -p 0 root@host", "ssh -p 65536 root@host", "ssh -p root@host",
                        "ssh root@host reboot", "ssh root@$(whoami)", "ssh -o ProxyCommand=evil root@host",
                        "ssh root@host\ntouch /tmp/should-not-exist"):
            self.assertNotEqual(self.run_script(command, "--dry-run").returncode, 0)
        self.assertNotEqual(self.run_script("ssh root@host", "-n", "../bad", "--dry-run").returncode, 0)
        self.assertFalse((self.local / ".ssh").exists())

    def test_interactive_default_and_custom_name(self):
        result = self.run_script("--dry-run", input_text="ssh -p 29925 root@connect.westx.seetacloud.com\n\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ssh westx-29925", result.stdout)
        result = self.run_script("ssh -p 2222 dev@[::1]", "--name", "gpu", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ssh gpu", result.stdout)

    def test_success_repeat_and_cross_version_key_deduplication(self):
        ssh_dir = self.local / ".ssh"
        ssh_dir.mkdir()
        original = "# original\nHost *\n    Port 1234\nHost old\n    HostName old.example\n"
        config = ssh_dir / "config"
        config.write_text(original)
        key_path = ssh_dir / "id_ed25519_westx-29925"
        public_key = setup_ssh.ensure_key(key_path, "westx-29925")
        private_bytes = key_path.read_bytes()
        auth = self.remote / ".ssh" / "authorized_keys"
        auth.parent.mkdir()
        auth.write_text(public_key + "\nother-key-without-newline")
        for _ in range(2):
            result = self.run_script("ssh -p 29925 root@connect.westx.seetacloud.com")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("配置完成", result.stdout)
        self.assertEqual(key_path.read_bytes(), private_bytes)
        self.assertEqual(config.read_text().count("Host westx-29925\n"), 1)
        self.assertTrue(config.read_text().endswith(original))
        backups = list(ssh_dir.glob("config.backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), original)
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(auth.read_text().splitlines(), [public_key, "other-key-without-newline"])
        self.assertEqual(auth.stat().st_mode & 0o777, 0o600)
        calls = [json.loads(line) for line in (self.root / "calls").read_text().splitlines()]
        self.assertEqual(len(calls), 6)
        self.assertIn("BatchMode=yes", calls[1])
        self.assertIn("BatchMode=yes", calls[2])
        self.assertFalse(list(ssh_dir.glob(".setup-ssh.*")))
        # 真实 OpenSSH 只解析生成的配置，不建立连接。
        result = subprocess.run([shutil.which("ssh"), "-G", "-F", str(config), "westx-29925"],
                                capture_output=True, text=True, check=True)
        self.assertIn("port 29925\n", result.stdout)

    def test_remote_install_preserves_existing_key_and_adds_once(self):
        auth = self.remote / ".ssh" / "authorized_keys"
        auth.parent.mkdir()
        auth.write_text("existing-without-newline")
        for _ in range(2):
            result = self.run_script("ssh root@example.com", "-n", "new-key")
            self.assertEqual(result.returncode, 0, result.stderr)
        lines = auth.read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], "existing-without-newline")
        self.assertTrue(lines[1].startswith("ssh-ed25519 "))

    def test_failures_preserve_config(self):
        ssh_dir = self.local / ".ssh"
        ssh_dir.mkdir()
        config = ssh_dir / "config"
        original = "Host old\n    HostName old.example\n"
        for failure in ("FAIL_VERIFY", "FAIL_ALIAS", "MUTATE_CONFIG"):
            with self.subTest(failure=failure):
                config.write_text(original)
                result = self.run_script("ssh root@example.com", "-n", "gpu", env={**self.env, failure: "1"})
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(config.read_text(), "concurrent edit\n" if failure == "MUTATE_CONFIG" else original)
                self.assertFalse(list(ssh_dir.glob("config.backup-*")))
                self.assertFalse(list(ssh_dir.glob(".setup-ssh.*")))

    def test_conflicts_stop_before_key_generation_or_connection(self):
        ssh_dir = self.local / ".ssh"
        ssh_dir.mkdir()
        config = ssh_dir / "config"
        for text in ("Host gpu\n    HostName old\n", "# BEGIN setup_ssh gpu\n"):
            config.write_text(text)
            result = self.run_script("ssh root@example.com", "-n", "gpu")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(config.read_text(), text)
            self.assertFalse((ssh_dir / "id_ed25519_gpu").exists())
            self.assertFalse((self.root / "calls").exists())


if __name__ == "__main__":
    unittest.main()
