"""PowerShell 语法、命令解析及原生进程参数测试；不连接服务器。

macOS 上也可运行这些检查；Windows ACL 和实际 SSH 连接需要 Windows 实机验证。
可通过 POWERSHELL_EXE 指定 pwsh / powershell.exe 路径。
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent
POWERSHELL = os.environ.get("POWERSHELL_EXE") or shutil.which("pwsh") or shutil.which("powershell.exe")


def ps_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


@unittest.skipUnless(POWERSHELL, "没有 PowerShell 运行时")
class PowerShellSetupTests(unittest.TestCase):
    def run_script(self, command, *args):
        return subprocess.run([POWERSHELL, "-NoLogo", "-NoProfile", "-File", str(ROOT / "setup_ssh.ps1"),
                               command, "-DryRun", *args], capture_output=True, text=True, encoding="utf-8")

    def test_default_and_custom_name(self):
        for command in (r"ssh -p 29925 root\@connect.westx.seetacloud.com",
                        "ssh -p29925 -l root connect.westx.seetacloud.com"):
            result = self.run_script(command)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ssh westx-29925", result.stdout)
        result = self.run_script("ssh -p 2222 dev@[::1]", "-Name", "my-gpu")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ssh my-gpu", result.stdout)

    def test_rejects_shell_code_bad_port_and_bad_alias(self):
        for command in ("ssh -p 65536 root@host", "ssh root@$(whoami)", "ssh root@host reboot",
                        "ssh -o ProxyCommand=evil root@host", "ssh root@host\nwhoami", "ssh -p"):
            result = self.run_script(command)
            self.assertNotEqual(result.returncode, 0, command)
        self.assertNotEqual(self.run_script("ssh root@host", "-Name", "../bad").returncode, 0)

    def test_parser_and_native_empty_arguments_quotes_spaces_and_stdin(self):
        # 只加载函数定义；不会执行主流程或写入真实用户配置。
        native_args = ["", 'a"b', "C:\\user name\\folder\\", "$(echo nope); 'quoted'", "普通文本"]
        python_code = "import json, sys; print(json.dumps([sys.argv[1:], sys.stdin.read()], ensure_ascii=False))"
        stdin_text = "first line\nsecond line\n"
        arguments = ", ".join(ps_literal(value) for value in ["-c", python_code, *native_args])
        helper = f"""
$ErrorActionPreference = 'Stop'
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$tokens = $null; $parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile({ps_literal(ROOT / 'setup_ssh.ps1')}, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) {{ throw ($parseErrors | Out-String) }}
foreach ($definition in $ast.FindAll({{ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] }}, $false)) {{
    . ([scriptblock]::Create($definition.Extent.Text))
}}
Invoke-Native -Exe {ps_literal(sys.executable)} -Arguments @({arguments}) -StdinText {ps_literal(stdin_text)} -Capture
"""
        with tempfile.TemporaryDirectory() as directory:
            helper_path = Path(directory) / "test native args.ps1"
            helper_path.write_text(helper, encoding="utf-8-sig")
            result = subprocess.run([POWERSHELL, "-NoLogo", "-NoProfile", "-File", str(helper_path)],
                                    capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [native_args, stdin_text])


if __name__ == "__main__":
    unittest.main()
