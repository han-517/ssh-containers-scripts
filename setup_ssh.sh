#!/usr/bin/env bash
# macOS / Linux / WSL；Bash 3.2+，不依赖 Python 或 ssh-copy-id。
set -euo pipefail
umask 077

fail() { printf '错误：%s\n' "$*" >&2; exit 1; }
usage() {
    cat <<'EOF'
用法：bash setup_ssh.sh ["ssh -p 29925 root@connect.westx.seetacloud.com"] [-n 别名] [--dry-run]
省略命令时交互输入；默认别名 westx-29925，可自定义。
生成无口令 Ed25519 密钥；密码由 SSH 在终端询问，不保存。
支持 -p、-l，不接受其他 SSH 参数或远程命令。
EOF
}

ssh_command=''; name=''; dry_run=0; interactive=0
while (($#)); do
    case "$1" in
        -n|--name) (($# >= 2)) || fail "$1 缺少参数"; name=$2; shift 2 ;;
        --dry-run) dry_run=1; shift ;;
        -h|--help) usage; exit 0 ;;
        -*) fail "未知参数：$1" ;;
        *) [[ -z "$ssh_command" ]] || fail '完整 SSH 命令请用引号包住'; ssh_command=$1; shift ;;
    esac
done
if [[ -z "$ssh_command" ]]; then
    interactive=1
    read -r -p '粘贴 SSH 命令：' ssh_command || fail '未输入 SSH 命令'
fi
[[ "$ssh_command" != *$'\n'* && "$ssh_command" != *$'\r'* ]] || fail 'SSH 命令必须只有一行'
ssh_command=${ssh_command//\\@/@}
read -r -a words <<< "$ssh_command"
[[ ${words[0]:-} == ssh ]] || fail '请输入以 ssh 开头的命令'
server=''; login_user=''; port=22; i=1
while ((i < ${#words[@]})); do
    token=${words[$i]}; option=''; value=''
    case "$token" in
        -p|-l)
            option=$token; i=$((i + 1))
            ((i < ${#words[@]})) || fail "$token 缺少参数"
            value=${words[$i]} ;;
        -p?*|-l?*) option=${token:0:2}; value=${token:2} ;;
        -*) fail "不支持 SSH 参数 ${token}；只接受 -p、-l 和 用户@主机" ;;
        *) [[ -z "$server" ]] || fail '只接受一个 SSH 地址，不接受远程命令'; server=$token ;;
    esac
    case "$option" in -p) port=$value ;; -l) login_user=$value ;; esac
    i=$((i + 1))
done
if [[ "$server" == *@* ]]; then
    address_user=${server%@*}; server=${server##*@}
    [[ -z "$login_user" || "$login_user" == "$address_user" ]] || fail '两处用户名不同'
    login_user=$address_user
fi
[[ "$server" != \[*\] ]] || server=${server:1:${#server}-2}
[[ "$login_user" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]*$ ]] || fail '请提供有效用户名，例如 root@主机'
[[ "$server" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ || "$server" =~ ^[A-Fa-f0-9:]+:[A-Fa-f0-9:.]*$ ]] || fail '主机地址无效'
[[ "$port" =~ ^[0-9]{1,5}$ ]] || fail '端口必须为 1–65535'
port=$((10#$port)); ((port >= 1 && port <= 65535)) || fail '端口必须为 1–65535'
if [[ "$server" == *:* ]]; then
    label=${server//:/-}
    while [[ "$label" == -* ]]; do label=${label#-}; done
    while [[ "$label" == *- ]]; do label=${label%-}; done
elif [[ "$server" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then label=$server
elif [[ "$server" == connect.* ]]; then label=${server#connect.}; label=${label%%.*}
else label=${server%%.*}
fi
default_name=${label:-server}-$port
if [[ -z "$name" && "$interactive" == 1 ]]; then
    read -r -p "登录别名 [$default_name]（回车使用默认）：" name || fail '未输入别名'
fi
name=${name:-$default_name}
[[ "$name" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$ ]] || fail '别名须为 1–64 位字母、数字、点、下划线或短横线，且以字母或数字开头'
printf '目标：%s@%s:%s\n别名：%s\n' "$login_user" "$server" "$port" "$name"
if [[ "$dry_run" == 1 ]]; then printf '登录命令：ssh %s\n' "$name"; exit 0; fi

for tool in ssh ssh-keygen awk mktemp cmp; do command -v "$tool" >/dev/null || fail "找不到 $tool"; done
ssh_dir=$HOME/.ssh; config=$ssh_dir/config; key=$ssh_dir/id_ed25519_$name
[[ ! -L "$config" && ! -L "$key" && ! -L "$key.pub" ]] || fail '配置或密钥不能是符号链接'
mkdir -p "$ssh_dir"; chmod 700 "$ssh_dir"
work=$(mktemp -d "$ssh_dir/.setup-ssh.XXXXXX")
trap 'rm -rf -- "$work"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
had_config=0
if [[ -e "$config" ]]; then had_config=1; cp "$config" "$work/original"; else : > "$work/original"; fi

# 只删除同名的脚本配置块；其他内容保留。异常标记或手动同名 Host 会中止。
if ! awk -v alias="$name" '
BEGIN { begin="# BEGIN setup_ssh " alias; end="# END setup_ssh " alias }
{
    line=$0; sub(/\r$/, "", line)
    if (line==begin) { if (inside || starts++) bad=1; inside=1; next }
    if (line==end) { if (!inside || ends++) bad=1; inside=0; next }
    if (inside) next
    check=line; sub(/#.*/, "", check); sub(/^[ \t]+/, "", check)
    count=split(check, fields, /[ \t=]+/)
    if (tolower(fields[1])=="host") for (i=2; i<=count; i++) {
        gsub(/"/, "", fields[i]); if (tolower(fields[i])==tolower(alias)) bad=1
    }
    print $0
}
END { if (inside || starts!=ends || bad) exit 1 }
' "$work/original" > "$work/rest"; then
    fail '已有手动配置的同名 Host，或脚本配置标记不完整；请检查配置或更换别名'
fi
# 使用 ~ 路径，兼容用户目录包含空格或特殊字符。
cat > "$work/config" <<EOF
# BEGIN setup_ssh $name
Host $name
    HostName $server
    User $login_user
    Port $port
    IdentityFile "~/.ssh/id_ed25519_$name"
    IdentitiesOnly yes
    ServerAliveInterval 30
    ServerAliveCountMax 3
Host *
# END setup_ssh $name
EOF
cat "$work/rest" >> "$work/config"

if [[ ! -e "$key" ]]; then
    [[ ! -e "$key.pub" ]] || fail '公钥已存在但私钥缺失，请换一个别名'
    ssh-keygen -q -t ed25519 -N '' -C "setup_ssh:$name" -f "$key"
fi
chmod 600 "$key"
public_key=$(ssh-keygen -y -P '' -f "$key") || fail '无法读取私钥；请换一个别名或检查私钥口令'
# 只取算法和密钥主体，使可选注释不会进入远程 shell。
read -r algorithm key_data ignored <<< "$public_key"
[[ "$algorithm" == ssh-ed25519 && "$key_data" =~ ^[A-Za-z0-9+/=]+$ ]] || fail '已有密钥不是有效的 Ed25519 密钥'
public_key="$algorithm $key_data"
printf '%s\n' "$public_key" > "$key.pub"; chmod 644 "$key.pub"
printf '本地私钥：%s（无口令，权限 600）\n' "$key"
cat > "$work/install.sh" <<EOF
set -eu
umask 077
mkdir -p "\$HOME/.ssh"
chmod 700 "\$HOME/.ssh"
auth="\$HOME/.ssh/authorized_keys"
touch "\$auth"
chmod 600 "\$auth"
key='$public_key'
if awk -v wanted='$key_data' '\$1 == "ssh-ed25519" && \$2 == wanted { found=1 } END { exit !found }' "\$auth"; then
    :
else
    status=\$?
    [ "\$status" -eq 1 ] || exit "\$status"
    if [ -s "\$auth" ]; then printf '\\n' >> "\$auth"; fi
    printf '%s\\n' "\$key" >> "\$auth"
fi
EOF
base=(ssh -F /dev/null -T -p "$port" -l "$login_user" -i "$key"
    -o IdentitiesOnly=yes -o IdentityAgent=none -o StrictHostKeyChecking=ask
    -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=3)
printf '安装公钥：请按 SSH 提示确认首次连接指纹，并输入服务器密码（输入时不显示）。\n'
"${base[@]}" "$server" 'sh -s' < "$work/install.sh" || fail '公钥安装失败，未更新 SSH 配置，可重试'
printf '验证密钥登录…\n'
"${base[@]}" -o BatchMode=yes -o PreferredAuthentications=publickey "$server" true || fail '密钥登录失败，未更新 SSH 配置'
ssh -F "$work/config" -T -o BatchMode=yes -o PreferredAuthentications=publickey \
    -o ControlMaster=no -o ControlPath=none -o ConnectTimeout=15 "$name" true || fail '别名登录验证失败，未更新 SSH 配置'
[[ ! -L "$config" ]] || fail '配置在安装期间变成了符号链接，请重新运行'
if [[ "$had_config" == 1 ]]; then
    cmp -s "$config" "$work/original" || fail '配置在安装期间有修改，未覆盖，请重新运行'
else
    [[ ! -e "$config" ]] || fail '配置在安装期间被创建，未覆盖，请重新运行'
fi
if [[ "$had_config" == 0 ]] || ! cmp -s "$config" "$work/config"; then
    if [[ "$had_config" == 1 ]]; then
        backup=$(mktemp "$ssh_dir/config.backup-XXXXXX")
        cat "$work/original" > "$backup"
        printf '原配置备份：%s\n' "$backup"
    fi
    mv -f "$work/config" "$config"
fi
chmod 600 "$config"
printf '\n配置完成！以后运行：ssh %s\n' "$name"
