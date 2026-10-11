# SSH 快速登录脚本

粘贴 `ssh -p 29925 root@connect.westx.seetacloud.com`，按提示输入服务器密码，即可创建密钥并配置登录别名。默认别名为 `westx-29925`，也可自定义。示例中的 `root\@` 同样支持。

| 本机环境 | 文件 | 依赖 |
| --- | --- | --- |
| macOS / Linux / WSL | `setup_ssh.sh` | Bash 3.2+、OpenSSH 客户端、系统常用命令 |
| Windows PowerShell 5.1 / PowerShell 7 | `setup_ssh.ps1` | OpenSSH 客户端，不需要 Python |
| Windows CMD | `setup_ssh.cmd` | 同目录的 `setup_ssh.ps1`，调用 Windows PowerShell |
| macOS / Linux | `setup_ssh.py` | Python 3.9+、OpenSSH 客户端 |

远端目标是有 POSIX `sh`、`awk` 的 Linux 服务器或容器。无需在远端安装 Python。WSL 版配置的是 WSL 内的 SSH，Windows 原生版配置的是 Windows 用户的 SSH。

## 获取脚本

```bash
git clone https://github.com/han-517/ssh-containers-scripts.git
cd ssh-containers-scripts
```

也可以点击仓库页面的 **Code → Download ZIP**，解压后在文件夹中打开终端。

## Bash：macOS / Linux / WSL

在脚本所在目录交互运行：

```bash
bash ./setup_ssh.sh
```

直接传入命令，使用默认别名：

```bash
bash ./setup_ssh.sh 'ssh -p 29925 root@connect.westx.seetacloud.com'
```

自定义名称：

```bash
bash ./setup_ssh.sh 'ssh -p 29925 root@connect.westx.seetacloud.com' --name my-gpu
```

## Windows PowerShell

把 `setup_ssh.ps1` 放到本机，在所在目录执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup_ssh.ps1
```

直接传入命令或指定别名：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup_ssh.ps1 "ssh -p 29925 root@connect.westx.seetacloud.com"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup_ssh.ps1 "ssh -p 29925 root@connect.westx.seetacloud.com" -Name my-gpu
```

已安装 PowerShell 7 时，可以把命令开头的 `powershell.exe` 改成 `pwsh`。这里的执行策略参数仅用于启动这次脚本，不修改永久执行策略。缺少 `ssh.exe` 或 `ssh-keygen.exe` 时，在 Windows 的“可选功能”中安装 OpenSSH 客户端。

## Windows CMD

把 `setup_ssh.cmd` 和 `setup_ssh.ps1` 放在同一目录。在 CMD 中运行：

```bat
setup_ssh.cmd
setup_ssh.cmd "ssh -p 29925 root@connect.westx.seetacloud.com" -Name my-gpu
```

CMD 文件是 PowerShell 版的启动入口。

## Python

```bash
python3 ./setup_ssh.py
python3 ./setup_ssh.py 'ssh -p 29925 root@connect.westx.seetacloud.com' --name my-gpu
```

## 使用结果

首次连接可能需要确认服务器指纹，然后按 SSH 的提示输入服务器密码；密码输入时不显示，也不保存。配置成功后：

```text
ssh westx-29925
```

若指定了 `my-gpu`，则使用 `ssh my-gpu`。

- 每个别名对应一份无口令 Ed25519 私钥，保存在本机 `~/.ssh/id_ed25519_<别名>`；Windows 的 `~` 指用户目录。仅公钥上传到远端 `~/.ssh/authorized_keys`。
- 三版共用密钥命名和配置块标记。同一本机、同一用户下重复运行会复用密钥；不同机器各自生成密钥。
- 公钥按密钥主体去重，保留已有授权密钥。脚本会修正远端 `.ssh` 和 `authorized_keys` 权限。
- 密钥登录及别名登录都验证成功后，才更新本地 `~/.ssh/config`；原配置备份为 `config.backup-*`。安装或验证失败时可直接重试。
- 手动维护的同名 `Host` 会提示换名；安装期间本地配置发生变化则停止写入。
- 如果容器重建后丢失公钥，可重新运行。若 SSH 提示主机指纹变化，需要先核实新指纹并处理旧记录；脚本保留 SSH 的主机验证。

脚本接收简单直连命令，支持 `-p 29925` / `-p29925`、`user@host` / `-l user host`，不接收 `-o`、跳板机或远程命令。

## 只预览，不连接

```bash
bash ./setup_ssh.sh 'ssh -p 29925 root@connect.westx.seetacloud.com' --dry-run
python3 ./setup_ssh.py 'ssh -p 29925 root@connect.westx.seetacloud.com' --dry-run
```

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup_ssh.ps1 "ssh -p 29925 root@connect.westx.seetacloud.com" -DryRun
```

## 离线测试

```bash
python3 -m unittest -v test_setup_ssh.py test_setup_ssh_shell.py test_setup_ssh_powershell.py
```

测试使用临时目录和 SSH 替身，不连接真实服务器，也不修改实际用户的 `.ssh`。覆盖真实密钥生成、重复安装、配置备份、别名冲突和失败时保留配置。PowerShell 测试需要本机有 `pwsh` / `powershell.exe`，也可通过 `POWERSHELL_EXE` 指定路径；没有运行时则跳过。

PowerShell 的语法、参数解析和原生进程调用可以在 macOS 检查；Windows ACL 和 Windows 实际 SSH 登录仍需在 Windows 实机验证。

实现参考：[PowerShell 原生命令参数解析](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_parsing)、[Windows OpenSSH 私钥权限](https://github.com/PowerShell/Win32-OpenSSH/wiki/Security-protection-of-various-files-in-win32-openssh)。
