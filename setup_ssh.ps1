#requires -Version 5.1
<#
.SYNOPSIS
安装 Linux 容器的 SSH 公钥，并创建本机登录别名。
.EXAMPLE
.\setup_ssh.ps1
.EXAMPLE
.\setup_ssh.ps1 "ssh -p 29925 root@connect.westx.seetacloud.com" -Name gpu
.NOTES
支持 Windows PowerShell 5.1 / PowerShell 7，需要 OpenSSH 客户端。
默认生成无口令 Ed25519 密钥，密码由 SSH 终端提示输入，不保存。
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)][string]$SshCommand,
    [Alias('n')][string]$Name,
    [switch]$DryRun
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

# 使用直接进程调用，兼容 Windows PowerShell 5.1 对空字符串参数的处理。
# 按 Windows 原生命令行规则转义；不经 cmd / Invoke-Expression 执行用户输入。
function ConvertTo-NativeArgument([AllowEmptyString()][string]$Value) {
    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

function Invoke-Native {
    param([string]$Exe, [string[]]$Arguments, [string]$StdinText, [switch]$Capture)
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $Exe
    $info.Arguments = (($Arguments | ForEach-Object { ConvertTo-NativeArgument $_ }) -join ' ')
    $info.UseShellExecute = $false
    $info.RedirectStandardOutput = [bool]$Capture
    $info.RedirectStandardInput = $PSBoundParameters.ContainsKey('StdinText')
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $info
    try {
        [void]$process.Start()
        if ($info.RedirectStandardInput) {
            # .NET Framework (Windows PowerShell 5.1) has no StandardInputEncoding property.
            $writer = New-Object System.IO.StreamWriter($process.StandardInput.BaseStream, $utf8NoBom)
            try { $writer.Write($StdinText) } finally { $writer.Dispose() }
        }
        $outputText = if ($Capture) { $process.StandardOutput.ReadToEnd() } else { '' }
        $process.WaitForExit()
        if ($process.ExitCode -ne 0) { throw "$Exe 执行失败，退出码 $($process.ExitCode)。" }
        if ($Capture) { return $outputText.Trim() }
    } finally { $process.Dispose() }
}

function Protect-PrivateFile([string]$Path) {
    $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl = New-Object System.Security.AccessControl.FileSecurity
    # OpenSSH keys may grant their owner Modify but not WRITE_OWNER.
    # Owners can still update the DACL; do not request an unnecessary owner change.
    $owner = (Get-Acl -LiteralPath $Path).GetOwner([System.Security.Principal.SecurityIdentifier])
    if ($owner -ne $sid) { $acl.SetOwner($sid) }
    $acl.SetAccessRuleProtection($true, $false)
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', 'Allow')
    [void]$acl.AddAccessRule($rule)
    # Set-Acl can require SeSecurityPrivilege when protecting an already protected file.
    # Persist only the modified owner and access rules, leaving audit settings untouched.
    if ($PSVersionTable.PSEdition -eq 'Desktop') {
        [System.IO.File]::SetAccessControl($Path, $acl)
    } else {
        [System.IO.FileSystemAclExtensions]::SetAccessControl([System.IO.FileInfo]::new($Path), $acl)
    }
}

function Read-SSHConfig([string]$Path) {
    if (Test-Path -LiteralPath $Path) {
        if ((Get-Item -LiteralPath $Path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'SSH 配置不能是符号链接。'
        }
        return [IO.File]::ReadAllText($Path)
    }
    return ''
}

$workDirectory = $null
try {
    $interactive = [string]::IsNullOrWhiteSpace($SshCommand)
    if ($interactive) { $SshCommand = Read-Host '粘贴 SSH 命令' }
    if ($SshCommand -match '[\r\n]') { throw 'SSH 命令必须只有一行。' }
    $tokens = @($SshCommand.Replace('\@', '@').Trim() -split '\s+')
    if ($tokens.Count -eq 0 -or $tokens[0] -cne 'ssh') { throw '请输入以 ssh 开头的命令。' }
    $server = ''; $loginUser = ''; $portText = '22'
    for ($index = 1; $index -lt $tokens.Count; $index++) {
        $token = $tokens[$index]; $option = ''; $value = ''
        if ($token -ceq '-p' -or $token -ceq '-l') {
            $option = $token; $index++
            if ($index -ge $tokens.Count) { throw "$token 缺少参数。" }
            $value = $tokens[$index]
        } elseif ($token -cmatch '^-(p|l).+') {
            $option = $token.Substring(0, 2); $value = $token.Substring(2)
        } elseif ($token.StartsWith('-')) { throw "不支持参数 ${token}；只接受 -p、-l 和 用户@主机。"
        } else {
            if ($server) { throw '只接受一个 SSH 地址，不接受远程命令。' }
            $server = $token
        }
        if ($option -ceq '-p') { $portText = $value }
        if ($option -ceq '-l') { $loginUser = $value }
    }
    if ($server.Contains('@')) {
        $atIndex = $server.LastIndexOf('@')
        $addressUser = $server.Substring(0, $atIndex)
        $server = $server.Substring($atIndex + 1)
        if ($loginUser -and $loginUser -cne $addressUser) { throw '两处用户名不同。' }
        $loginUser = $addressUser
    }
    if ($server.StartsWith('[') -and $server.EndsWith(']')) { $server = $server.Substring(1, $server.Length - 2) }
    if ($loginUser -cnotmatch '^[A-Za-z0-9_][A-Za-z0-9_.-]*$') { throw '请提供有效用户名，例如 root@主机。' }
    if ($server.Contains(':')) {
        $ip = $null
        if (-not [Net.IPAddress]::TryParse($server, [ref]$ip)) { throw 'IPv6 地址无效。' }
    } elseif ($server -cnotmatch '^[A-Za-z0-9][A-Za-z0-9.-]*$') { throw '主机地址无效。' }
    if ($portText -notmatch '^[0-9]{1,5}$') { throw '端口必须为 1–65535。' }
    $portNumber = [int]$portText
    if ($portNumber -lt 1 -or $portNumber -gt 65535) { throw '端口必须为 1–65535。' }
    if ($server.Contains(':')) { $label = $server.Replace(':', '-').Trim('-')
    } elseif ($server -match '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$') { $label = $server
    } elseif ($server.StartsWith('connect.')) { $label = $server.Split('.')[1]
    } else { $label = $server.Split('.')[0] }
    if (-not $label) { $label = 'server' }
    $defaultName = "$label-$portNumber"
    if (-not $Name -and $interactive) { $Name = Read-Host "登录别名 [$defaultName]（回车使用默认）" }
    if (-not $Name) { $Name = $defaultName }
    if ($Name -cnotmatch '^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$') {
        throw '别名须为 1–64 位字母、数字、点、下划线或短横线，且以字母或数字开头。'
    }
    Write-Host "目标：${loginUser}@${server}:$portNumber`n别名：$Name"
    if ($DryRun) { Write-Host "登录命令：ssh $Name"; exit 0 }
    if ([Environment]::OSVersion.Platform -ne 'Win32NT') { throw '此版本用于 Windows；macOS / Linux 请使用 Bash 或 Python 版。' }
    $sshExe = (Get-Command ssh.exe -CommandType Application -ErrorAction Stop).Source
    $keygenExe = (Get-Command ssh-keygen.exe -CommandType Application -ErrorAction Stop).Source
    $profileDirectory = [Environment]::GetFolderPath('UserProfile')
    $sshDirectory = Join-Path $profileDirectory '.ssh'
    $configPath = Join-Path $sshDirectory 'config'
    $keyPath = Join-Path $sshDirectory "id_ed25519_$Name"
    $original = Read-SSHConfig $configPath
    $hadConfig = Test-Path -LiteralPath $configPath
    foreach ($path in @($keyPath, "$keyPath.pub")) {
        if ((Test-Path -LiteralPath $path) -and ((Get-Item -LiteralPath $path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw '密钥不能是符号链接。'
        }
    }
    $begin = "# BEGIN setup_ssh $Name"; $end = "# END setup_ssh $Name"
    $beginMatches = [regex]::Matches($original, '(?m)^' + [regex]::Escape($begin) + '\r?$')
    $endMatches = [regex]::Matches($original, '(?m)^' + [regex]::Escape($end) + '\r?$')
    $remaining = $original
    if ($beginMatches.Count -or $endMatches.Count) {
        if ($beginMatches.Count -ne 1 -or $endMatches.Count -ne 1 -or $beginMatches[0].Index -ge $endMatches[0].Index) {
            throw 'SSH 配置中的脚本标记不完整，请先检查配置。'
        }
        $blockEnd = $endMatches[0].Index + $endMatches[0].Length
        if ($blockEnd -lt $original.Length -and $original[$blockEnd] -eq "`n") { $blockEnd++ }
        $remaining = $original.Remove($beginMatches[0].Index, $blockEnd - $beginMatches[0].Index)
    }
    foreach ($line in ($remaining -split '\r?\n')) {
        $fields = @(($line -replace '#.*$', '').Trim() -split '[\s=]+')
        if ($fields[0] -ieq 'host') {
            foreach ($field in ($fields | Select-Object -Skip 1)) {
                if ($field.Trim('"') -ieq $Name) { throw "已有手动配置的 Host ${Name}，请用 -Name 更换名称。" }
            }
        }
    }
    $block = @"
$begin
Host $Name
    HostName $server
    User $loginUser
    Port $portNumber
    IdentityFile "~/.ssh/id_ed25519_$Name"
    IdentitiesOnly yes
    ServerAliveInterval 30
    ServerAliveCountMax 3
Host *
$end
"@
    $updated = ($block -replace "`r`n", "`n") + "`n" + $remaining
    [void][IO.Directory]::CreateDirectory($sshDirectory)
    $workDirectory = Join-Path $sshDirectory ('.setup-ssh-' + [guid]::NewGuid().ToString('N'))
    [void][IO.Directory]::CreateDirectory($workDirectory)
    $emptyConfig = Join-Path $workDirectory 'empty-config'
    $stagedConfig = Join-Path $workDirectory 'config'
    [IO.File]::WriteAllText($emptyConfig, '', $utf8NoBom)
    [IO.File]::WriteAllText($stagedConfig, $updated, $utf8NoBom)
    Protect-PrivateFile $stagedConfig
    if (-not (Test-Path -LiteralPath $keyPath)) {
        if (Test-Path -LiteralPath "$keyPath.pub") { throw '公钥已存在但私钥缺失，请换一个别名。' }
        Invoke-Native -Exe $keygenExe -Arguments @('-q', '-t', 'ed25519', '-N', '', '-C', "setup_ssh:$Name", '-f', $keyPath)
    }
    Protect-PrivateFile $keyPath
    $publicKey = Invoke-Native -Exe $keygenExe -Arguments @('-y', '-P', '', '-f', $keyPath) -Capture
    if ($publicKey -cnotmatch '^ssh-ed25519 ([A-Za-z0-9+/=]+)(?: [^\r\n]*)?$') { throw '已有密钥不是有效的 Ed25519 密钥。' }
    $keyData = $Matches[1]; $publicKey = "ssh-ed25519 $keyData"
    [IO.File]::WriteAllText("$keyPath.pub", $publicKey + "`n", $utf8NoBom)
    Write-Host "本地私钥：${keyPath}（无口令，仅当前用户可访问）"
    # 单引号 here-string 保留远端 shell 的 $；仅替换已验证的公钥主体。
    $remoteScript = @'
set -eu
umask 077
mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"
auth="$HOME/.ssh/authorized_keys"
touch "$auth"
chmod 600 "$auth"
key='ssh-ed25519 __KEY_DATA__'
if awk -v wanted='__KEY_DATA__' '$1 == "ssh-ed25519" && $2 == wanted { found=1 } END { exit !found }' "$auth"; then
    :
else
    status=$?
    [ "$status" -eq 1 ] || exit "$status"
    if [ -s "$auth" ]; then printf '\n' >> "$auth"; fi
    printf '%s\n' "$key" >> "$auth"
fi
'@
    $remoteScript = ($remoteScript.Replace('__KEY_DATA__', $keyData) -replace "`r`n", "`n") + "`n"
    $baseArgs = @('-F', $emptyConfig, '-T', '-p', "$portNumber", '-l', $loginUser, '-i', $keyPath,
        '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none', '-o', 'StrictHostKeyChecking=ask',
        '-o', 'ConnectTimeout=15', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3')
    Write-Host '安装公钥：请按 SSH 提示确认首次连接指纹，并输入服务器密码（输入时不显示）。'
    Invoke-Native -Exe $sshExe -Arguments ($baseArgs + @($server, 'sh -s')) -StdinText $remoteScript
    Write-Host '验证密钥登录…'
    Invoke-Native -Exe $sshExe -Arguments ($baseArgs + @('-o', 'BatchMode=yes', '-o', 'PreferredAuthentications=publickey', $server, 'true'))
    Invoke-Native -Exe $sshExe -Arguments @('-F', $stagedConfig, '-T', '-o', 'BatchMode=yes',
        '-o', 'PreferredAuthentications=publickey', '-o', 'ControlMaster=no', '-o', 'ControlPath=none',
        '-o', 'ConnectTimeout=15', $Name, 'true')
    if ((Read-SSHConfig $configPath) -cne $original -or (Test-Path -LiteralPath $configPath) -ne $hadConfig) {
        throw 'SSH 配置在安装期间有修改，未覆盖，请重新运行。'
    }
    if ($updated -cne $original -or -not $hadConfig) {
        if ($hadConfig) {
            $backup = Join-Path $sshDirectory ('config.backup-' + [guid]::NewGuid().ToString('N'))
            [IO.File]::Replace($stagedConfig, $configPath, $backup)
            Protect-PrivateFile $backup
            Write-Host "原配置备份：$backup"
        } else { [IO.File]::Move($stagedConfig, $configPath) }
    }
    Protect-PrivateFile $configPath
    Write-Host "`n配置完成！以后运行：ssh $Name"
} catch {
    [Console]::Error.WriteLine("错误：$($_.Exception.Message)")
    [Console]::Error.WriteLine('可以修复问题后重试；已创建的密钥和已安装的公钥会保留。')
    exit 1
} finally {
    if ($workDirectory -and (Test-Path -LiteralPath $workDirectory)) {
        Remove-Item -LiteralPath $workDirectory -Recurse -Force
    }
}
