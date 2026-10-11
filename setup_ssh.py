#!/usr/bin/env python3
"""粘贴 SSH 命令，安装登录公钥并创建本地 SSH 别名（macOS / Linux）。

示例：
  python3 setup_ssh.py
  python3 setup_ssh.py 'ssh -p 29925 root@connect.westx.seetacloud.com'
  python3 setup_ssh.py 'ssh -p 29925 root@connect.westx.seetacloud.com' -n gpu

仅依赖 Python 3.9+、ssh、ssh-keygen，以及远端的 POSIX sh / awk。
默认创建无口令 Ed25519 私钥，保存在 ~/.ssh/id_ed25519_<别名>，权限为 600。
服务器密码由 ssh 在终端中询问，脚本不读取、不保存密码。
"""

import argparse
from dataclasses import dataclass
import ipaddress
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile


@dataclass(frozen=True)
class Target:
    host: str
    user: str
    port: int

    @property
    def default_name(self):
        parts = self.host.split(".")
        try:
            ipaddress.ip_address(self.host)
            label = self.host.replace(":", "-")
        except ValueError:
            label = parts[1] if parts[0] == "connect" and len(parts) > 1 else parts[0]
        return f"{label.strip('-') or 'server'}-{self.port}"


def parse_ssh_command(command):
    # 只解析参数，绝不把用户粘贴的命令交给 shell 执行。
    tokens = shlex.split(command)
    if not tokens or tokens.pop(0) != "ssh":
        raise ValueError("请输入以 ssh 开头的命令。")
    host = user = None
    port = "22"
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in ("-p", "-l"):
            index += 1
            if index >= len(tokens):
                raise ValueError(f"{token} 缺少参数。")
            option, value = token, tokens[index]
        elif token.startswith(("-p", "-l")) and len(token) > 2:
            option, value = token[:2], token[2:]
        elif token.startswith("-"):
            raise ValueError(f"不支持参数 {token}；请只提供 -p、-l 和 用户@主机。")
        else:
            if host is not None:
                raise ValueError("请只提供一个 SSH 地址，不要包含远程命令或 shell 操作符。")
            host = token
            index += 1
            continue
        if option == "-p":
            port = value
        else:
            user = value
        index += 1
    if host is None:
        raise ValueError("命令缺少 SSH 地址。")
    if "@" in host:
        address_user, host = host.rsplit("@", 1)
        if user is not None and user != address_user:
            raise ValueError("-l 用户名与 用户@主机 中的用户名不同。")
        user = address_user
    if not user or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", user):
        raise ValueError("请明确提供有效用户名，例如 root@主机 或 -l root 主机。")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if ":" in host:
        ipaddress.IPv6Address(host)
    elif not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host):
        raise ValueError("主机名或 IP 地址无效。")
    if not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535:
        raise ValueError("端口必须在 1–65535 之间。")
    return Target(host, user, int(port))


def validate_name(name):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name):
        raise ValueError("别名须为 1–64 位字母、数字、点、下划线或短横线，并以字母或数字开头。")
    return name


def config_text(original, name, target, key_path):
    begin = f"# BEGIN setup_ssh {name}"
    end = f"# END setup_ssh {name}"
    lines = original.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.strip() == begin]
    ends = [i for i, line in enumerate(lines) if line.strip() == end]
    if starts or ends:
        if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
            raise ValueError(f"SSH 配置中 {name} 的脚本标记不完整，请先检查配置。")
        del lines[starts[0]:ends[0] + 1]
    # 不覆盖用户自己维护的同名 Host；脚本自己的块可重复更新。
    for line in lines:
        fields = re.split(r"[\s=]+", line.strip().split("#", 1)[0])
        if fields and fields[0].lower() == "host":
            if name.lower() in [field.strip('\"').lower() for field in fields[1:]]:
                raise ValueError(f"~/.ssh/config 已有手动配置的 Host {name}，请用 --name 换一个名称。")
    # OpenSSH 按先出现的配置取值，放在 Host * 之前；末尾恢复全局匹配范围。
    identity = str(key_path).replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    block = (
        f"{begin}\n"
        f"Host {name}\n"
        f"    HostName {target.host}\n"
        f"    User {target.user}\n"
        f"    Port {target.port}\n"
        f'    IdentityFile "{identity}"\n'
        "    IdentitiesOnly yes\n"
        "    ServerAliveInterval 30\n"
        "    ServerAliveCountMax 3\n"
        "Host *\n"
        f"{end}\n"
    )
    return block + "".join(lines)


def read_config(path):
    if path.is_symlink():
        raise ValueError(f"{path} 是符号链接；请先改用普通配置文件。")
    return path.read_text(encoding="utf-8") if path.exists() else ""


def private_temp(directory, prefix, content):
    fd, filename = tempfile.mkstemp(dir=directory, prefix=prefix)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(content)
    return Path(filename)


def save_config(path, original, updated):
    # 安装远端公钥可能耗时；避免覆盖用户在等待期间所作的本地修改。
    if read_config(path) != original:
        raise ValueError("SSH 配置在安装期间发生变化，未覆盖；请重新运行脚本。")
    if updated == original:
        return None
    backup = private_temp(path.parent, "config.backup-", original) if path.exists() else None
    staged = private_temp(path.parent, ".config-", updated)
    try:
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)
    return backup


def ensure_key(path, name):
    if path.is_symlink() or path.with_suffix(path.suffix + ".pub").is_symlink():
        raise ValueError("密钥路径不能是符号链接。")
    if not path.exists():
        if Path(str(path) + ".pub").exists():
            raise ValueError(f"{path}.pub 已存在，但私钥缺失；请换一个别名，避免覆盖。")
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"setup_ssh:{name}", "-f", str(path)],
            check=True,
        )
    path.chmod(0o600)
    # 从实际私钥导出公钥，防止旧 .pub 文件不匹配。-P '' 避免加密私钥导致额外提示。
    public_key = subprocess.run(
        ["ssh-keygen", "-y", "-P", "", "-f", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    if not public_key.startswith("ssh-ed25519 ") or "\n" in public_key:
        raise ValueError("已有密钥不是有效的 Ed25519 密钥，请换一个别名。")
    Path(str(path) + ".pub").write_text(public_key + "\n", encoding="ascii")
    Path(str(path) + ".pub").chmod(0o644)
    return public_key


def install_script(public_key):
    key_data = public_key.split()[1]
    return """set -eu
umask 077
mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"
auth="$HOME/.ssh/authorized_keys"
touch "$auth"
chmod 600 "$auth"
key=""" + shlex.quote(public_key) + """
if awk -v wanted=""" + shlex.quote(key_data) + """ '$1 == "ssh-ed25519" && $2 == wanted { found=1 } END { exit !found }' "$auth"; then
    :
else
    status=$?
    [ "$status" -eq 1 ] || exit "$status"
    # 兼容已有文件的最后一行没有换行符。
    if [ -s "$auth" ]; then printf '\\n' >> "$auth"; fi
    printf '%s\\n' "$key" >> "$auth"
fi
"""


def ssh_args(target, key_path):
    return [
        "ssh", "-F", "/dev/null", "-T", "-p", str(target.port), "-l", target.user,
        "-i", str(key_path), "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
        "-o", "StrictHostKeyChecking=ask", "-o", "ConnectTimeout=15",
        "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
    ]


def setup(target, name, ssh_dir):
    for tool in ("ssh", "ssh-keygen"):
        if shutil.which(tool) is None:
            raise ValueError(f"找不到 {tool}，请安装 OpenSSH 客户端。")
    config = ssh_dir / "config"
    key_path = ssh_dir / f"id_ed25519_{name}"
    original = read_config(config)
    updated = config_text(original, name, target, key_path)
    ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    ssh_dir.chmod(0o700)
    print(f"目标：{target.user}@{target.host}:{target.port}\n别名：{name}", flush=True)
    print(f"本地私钥：{key_path}（无口令，权限 600）", flush=True)
    public_key = ensure_key(key_path, name)
    print("安装公钥：请按 SSH 提示确认首次连接的主机指纹，并输入服务器密码（输入时不显示）。", flush=True)
    base = ssh_args(target, key_path)
    subprocess.run(base + [target.host, "sh -s"], input=install_script(public_key), text=True, check=True)
    print("验证密钥登录…", flush=True)
    subprocess.run(
        base + ["-o", "BatchMode=yes", "-o", "PreferredAuthentications=publickey", target.host, "true"],
        check=True,
    )
    # 同时验证实际别名配置，避免现有 Include / Match / Proxy 设置导致配置不可用。
    staged = private_temp(ssh_dir, ".config-check-", updated)
    try:
        subprocess.run(
            ["ssh", "-F", str(staged), "-T", "-o", "BatchMode=yes", "-o", "PreferredAuthentications=publickey",
             "-o", "ControlMaster=no", "-o", "ControlPath=none", "-o", "ConnectTimeout=15", name, "true"],
            check=True,
        )
    finally:
        staged.unlink(missing_ok=True)
    backup = save_config(config, original, updated)
    if backup:
        print(f"原配置备份：{backup}")
    print(f"\n配置完成！以后运行：ssh {name}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", help="完整 SSH 命令，请用引号包住；省略则交互输入")
    parser.add_argument("-n", "--name", help="自定义登录别名，例如 gpu；默认由主机和端口生成")
    parser.add_argument("--dry-run", action="store_true", help="只查看解析结果，不创建密钥、不连接、不修改配置")
    args = parser.parse_args(argv)
    try:
        target = parse_ssh_command(args.command or input("粘贴 SSH 命令：").strip())
        name = args.name
        if name is None and args.command is None:
            name = input(f"登录别名 [{target.default_name}]（回车使用默认）：").strip()
        name = validate_name(name or target.default_name)
        if args.dry_run:
            print(f"目标：{target.user}@{target.host}:{target.port}\n别名：{name}\n登录命令：ssh {name}")
        else:
            setup(target, name, Path.home() / ".ssh")
        return 0
    except (ValueError, OSError, EOFError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
    except subprocess.CalledProcessError as exc:
        print(f"{exc.cmd[0]} 执行失败（退出码 {exc.returncode}），未更新 SSH 配置。", file=sys.stderr)
        if exc.stderr:
            print(exc.stderr.strip(), file=sys.stderr)
        print("可以修复连接/权限问题后重试；已有密钥和已安装的公钥会保留。", file=sys.stderr)
    except KeyboardInterrupt:
        print("\n已取消；如已创建密钥或安装公钥，它们会保留以便重试。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
