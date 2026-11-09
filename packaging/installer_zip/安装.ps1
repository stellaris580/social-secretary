# ============================================================================
# AI 社交秘书 安装脚本（脚本安装器，无第三方安装器依赖）
# ----------------------------------------------------------------------------
# 为什么用脚本安装器（2026-10-31 Lead 决策）：
#   - 本机 Windows 11 25H2 (26200) 上 Inno Setup 运行时无法初始化（exit=1/挂起无日志）；
#   - PyInstaller onefile 安装器在本机沙箱/安全软件环境下运行时解包失败（exit=-1）；
#   因此改为"ZIP + PowerShell 脚本"：完全受控、可读、可脚本化验证，双击 `安装.cmd` 即可。
#
# 双击入口：安装.cmd（调用本脚本）。命令行用法：
#   安装.ps1                                  交互安装（默认目录 = %LOCALAPPDATA%\Programs\SocialSecretary）
#   安装.ps1 -Silent -InstallDir <路径> -Desktop -NoLaunch   静默安装（自动化验证用）
#   安装.ps1 -Silent -SkipShortcuts -SkipRegistry            沙箱降级（只复制文件）
#   安装.ps1 -ResultPath <json 文件>          把结果 JSON 写到文件
#
# 覆盖安装 / 运行中安装（2026-11-01 P0 修复）：
#   - 安装前检测目标目录的 $appExeName 是否正在运行（同路径进程）：先 CloseMainWindow()，
#     10 秒未退出则 Stop-Process -Force；仍无法关闭则明确失败（exit 2）并提示用户先退出，
#     **绝不假成功**；
#   - 替换前最多等 30 秒文件锁释放；
#   - 替换后校验安装目录 exe 的 SHA-256 与 payload-manifest.json 的 app_exe_sha256 一致，
#     不一致立即回滚（恢复旧版本目录）并 exit 3，不写快捷方式/注册表；
#   - 覆盖安装成功后删除旧目录备份（<target>.previous）。
#
# 安装内容：
#   1) 复制 app\ 整个 onedir 到安装目录（先复制到临时目录，成功后原子替换；失败回滚）
#   2) 开始菜单快捷方式（默认创建）+ 桌面快捷方式（-Desktop 可选）
#   3) HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\SocialSecretary 卸载登记项
#      （Windows「设置 → 应用」可见；DisplayName/Version/Publisher/InstallLocation/
#       DisplayIcon/UninstallString/EstimatedSize/NoModify/NoRepair）
#   4) 安装完成后可选启动程序（-NoLaunch 可跳过）
#   用户数据目录 %LOCALAPPDATA%\SocialSecretary 与程序目录分离，安装不写入任何消息数据。
#
# 安全与权限：
#   - 不需要管理员权限（只写 HKCU 与当前用户目录，PerUser 安装）；
#   - 注册表/日志只写安装元数据（路径/版本/大小），绝不写消息内容或凭据；
#   - 任何一步失败都会回滚已创建的目录/快捷方式/注册表项，不留半成品。
#
# 本文件必须保存为 UTF-8 with BOM（Windows PowerShell 5.1 才能正确解析中文）。
# ============================================================================
[CmdletBinding()]
param(
    [switch]$Silent,
    [string]$InstallDir = "",
    [switch]$Desktop,
    [switch]$NoLaunch,
    [string]$ResultPath = "",
    [switch]$SkipShortcuts,
    [switch]$SkipRegistry,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$ts = Get-Date -Format "yyyyMMdd_HHmmss"
$scriptDir = $PSScriptRoot
$appSource = Join-Path $scriptDir "app"
$appExeName = "SocialSecretary.exe"
$appName = "AI 社交秘书"
$appVersion = "0.2.5"
$publisher = "SocialSecretary Project"
$uninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\SocialSecretary"

$result = [ordered]@{
    mode = "install"
    ok = $false
    exit_code = 1
    install_dir = $null
    install_source = $appSource
    file_count = 0
    bytes = 0
    start_menu_lnk = $null
    desktop_lnk = $null
    registry_key = $uninstallKey
    registry_written = $false
    launched = $false
    rollback = $false
    running_processes = @()
    running_processes_closed = $false
    sha256_verified = $false
    installed_exe_sha256 = $null
    expected_exe_sha256 = $null
    silent = [bool]$Silent
    error = $null
    started_at = (Get-Date).ToString("o")
}

function Write-Result {
    param([int]$ExitCode)
    $result.exit_code = $ExitCode
    $result.finished_at = (Get-Date).ToString("o")
    $json = $result | ConvertTo-Json -Depth 6
    Write-Output $json
    if ($ResultPath) {
        try {
            $dir = Split-Path -Parent $ResultPath
            if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
            $json | Set-Content -Path $ResultPath -Encoding UTF8
        } catch { }
    }
    exit $ExitCode
}

function Fail {
    param([string]$Message, [int]$ExitCode = 3)
    $result.ok = $false
    $result.error = $Message
    Write-Host "[失败] $Message" -ForegroundColor Red
    Write-Result -ExitCode $ExitCode
}

# --- 0) 前置校验 -----------------------------------------------------------
if (-not (Test-Path (Join-Path $appSource $appExeName))) {
    Fail "安装包不完整：找不到 app\$appExeName（请确认 ZIP 解压完整）" 2
}
if (-not $InstallDir) {
    $InstallDir = Join-Path $env:LOCALAPPDATA "Programs\SocialSecretary"
}
$target = [System.IO.Path]::GetFullPath($InstallDir)
$result.install_dir = $target
$staging = "$target.installing"
$backup = "$target.previous"
$createdShortcuts = New-Object System.Collections.Generic.List[string]
$registryWritten = $false
$targetSwapped = $false
$backupMade = $false

function Rollback {
    Write-Host "[回滚] 清理安装半成品…" -ForegroundColor Yellow
    if ($registryWritten) {
        try { Remove-Item -Path $uninstallKey -Recurse -Force -ErrorAction SilentlyContinue } catch { }
    }
    foreach ($lnk in $createdShortcuts) {
        try { Remove-Item -Path $lnk -Force -ErrorAction SilentlyContinue } catch { }
    }
    try {
        if (Test-Path $staging) { Remove-Item -Path $staging -Recurse -Force -ErrorAction SilentlyContinue }
        if ($targetSwapped -and (Test-Path $target)) {
            Remove-Item -Path $target -Recurse -Force -ErrorAction SilentlyContinue
        }
        # 覆盖安装失败：把旧版本目录恢复回去（避免用户只剩半成品）
        if ($backupMade -and (Test-Path $backup)) {
            if (Test-Path $target) { Remove-Item -Path $target -Recurse -Force -ErrorAction SilentlyContinue }
            Move-Item -Path $backup -Destination $target -Force -ErrorAction SilentlyContinue
            Write-Host "[回滚] 已恢复旧版本目录: $target" -ForegroundColor Yellow
        }
    } catch { }
    $result.rollback = $true
}

function Get-RunningAppProcesses {
    <# 返回"可执行文件路径 == 目标安装目录 exe"的进程（同用户可读路径时）。 #>
    param([string]$ExePath)
    $found = @()
    try { $norm = [System.IO.Path]::GetFullPath($ExePath).ToLowerInvariant() } catch { $norm = $ExePath.ToLowerInvariant() }
    foreach ($proc in @(Get-Process -Name "SocialSecretary" -ErrorAction SilentlyContinue)) {
        $procPath = $null
        try { $procPath = $proc.Path } catch { $procPath = $null }
        if (-not $procPath) { continue }
        try {
            if ([System.IO.Path]::GetFullPath($procPath).ToLowerInvariant() -eq $norm) { $found += $proc }
        } catch { }
    }
    return $found
}

$script:lastUnlockFailure = $null
function Wait-FileUnlocked {
    <# 等待文件可独占打开（旧进程退出后句柄释放）；返回是否成功。
       区分"被占用"（IO 共享冲突，可重试）与"无权限/沙箱限制"（立即失败，不空等 30s）。 #>
    param([string]$Path, [int]$TimeoutSeconds = 30)
    $script:lastUnlockFailure = $null
    if (-not (Test-Path $Path)) { return $true }
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $fs = [System.IO.File]::Open($Path, "Open", "ReadWrite", "None")
            $fs.Close()
            return $true
        } catch [System.UnauthorizedAccessException] {
            $script:lastUnlockFailure = "permission"
            return $false
        } catch {
            $script:lastUnlockFailure = "locked"
            Start-Sleep -Milliseconds 500
        }
    }
    return $false
}

function Remove-PathWithRetry {
    param([string]$Path, [int]$TimeoutSeconds = 30)
    if (-not (Test-Path $Path)) { return $true }
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try { Remove-Item -Path $Path -Recurse -Force -ErrorAction Stop; return $true } catch { Start-Sleep -Milliseconds 500 }
    }
    return -not (Test-Path $Path)
}

function Move-PathWithRetry {
    param([string]$Source, [string]$Destination, [int]$TimeoutSeconds = 30)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try { Move-Item -Path $Source -Destination $Destination -Force -ErrorAction Stop; return $true } catch { Start-Sleep -Milliseconds 500 }
    }
    return $false
}

Write-Host "== $appName $appVersion 安装程序 ==" -ForegroundColor Cyan
Write-Host "安装目录: $target"
Write-Host "来源目录: $appSource"

try {
    # --- 0b) 运行中检测（P0 修复：旧进程占用会导致"假成功"，装完仍是旧版） ----
    $targetExe = Join-Path $target $appExeName
    $running = @(Get-RunningAppProcesses -ExePath $targetExe)
    if ($running.Count -gt 0) {
        $pids = ($running | ForEach-Object { $_.Id }) -join ","
        $result.running_processes = @($running | ForEach-Object { [ordered]@{ pid = $_.Id; path = $targetExe } })
        Write-Host "[检查] 检测到正在运行的 $appName（PID: $pids），先正常关闭（最长等待 10 秒）…" -ForegroundColor Yellow
        foreach ($proc in $running) {
            try { $null = $proc.CloseMainWindow() } catch { }
        }
        $deadline = (Get-Date).AddSeconds(10)
        while ((Get-Date) -lt $deadline) {
            if (@(Get-RunningAppProcesses -ExePath $targetExe).Count -eq 0) { break }
            Start-Sleep -Milliseconds 500
        }
        $stillRunning = @(Get-RunningAppProcesses -ExePath $targetExe)
        if ($stillRunning.Count -gt 0) {
            Write-Host "[检查] 程序未响应正常关闭，强制结束（只读程序可安全关闭）…" -ForegroundColor Yellow
            foreach ($proc in $stillRunning) {
                try { Stop-Process -Id $proc.Id -Force -ErrorAction Stop } catch { }
            }
            Start-Sleep -Seconds 2
            $stillRunning = @(Get-RunningAppProcesses -ExePath $targetExe)
            if ($stillRunning.Count -gt 0) {
                $pids2 = ($stillRunning | ForEach-Object { $_.Id }) -join ","
                Fail "检测到正在运行的 $appName（PID: $pids2）无法关闭。请先手动退出 AI 社交秘书后重新安装（避免旧版本占用导致安装“假成功”）。" 2
            }
        }
        $result.running_processes_closed = $true
        Write-Host "[检查] 旧进程已关闭，继续安装" -ForegroundColor Green
    }
    if (-not (Wait-FileUnlocked -Path $targetExe -TimeoutSeconds 30)) {
        if ($script:lastUnlockFailure -eq "permission") {
            Fail "无权限写入安装目录（$target）。请确认以当前用户身份运行安装程序、关闭安全软件的「受控文件夹访问」，或更换安装目录后重试。" 2
        } else {
            Fail "安装目录中的 $appExeName 仍被占用（可能被其它程序/安全软件锁定）。请先关闭 AI 社交秘书或稍后重试。" 2
        }
    }

    # --- 1) 复制到 staging（失败可整体回滚） --------------------------------
    if ((Test-Path $target) -and -not $Force) {
        $existingExe = Join-Path $target $appExeName
        if (Test-Path $existingExe) {
            Write-Host "[检查] 检测到已安装版本，将覆盖安装（用户数据不会被动）"
        }
    }
    Write-Host "[1/4] 复制程序文件…"
    if (Test-Path $staging) { Remove-Item -Path $staging -Recurse -Force -ErrorAction SilentlyContinue }
    New-Item -ItemType Directory -Path $staging -Force | Out-Null
    Copy-Item -Path (Join-Path $appSource "*") -Destination $staging -Recurse -Force

    $files = @(Get-ChildItem -Path $staging -Recurse -File)
    $bytes = 0
    foreach ($f in $files) { $bytes += $f.Length }
    $result.file_count = $files.Count
    $result.bytes = [int64]$bytes
    Write-Host ("      已复制 {0} 个文件 / {1:N1} MiB" -f $files.Count, ($bytes / 1MB))

    # --- 2) 原子替换目标目录（旧目录先备份，校验失败可恢复） -----------------
    Write-Host "[2/4] 安装到目标目录…"
    $parent = Split-Path -Parent $target
    if ($parent -and -not (Test-Path $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
    if (Test-Path $backup) { Remove-PathWithRetry -Path $backup -TimeoutSeconds 10 | Out-Null }
    if (Test-Path $target) {
        if (-not (Move-PathWithRetry -Source $target -Destination $backup -TimeoutSeconds 30)) {
            throw "无法替换安装目录：旧版本目录被占用（$target）。请关闭 AI 社交秘书后重试。"
        }
        $backupMade = $true
    }
    if (-not (Move-PathWithRetry -Source $staging -Destination $target -TimeoutSeconds 30)) {
        throw "无法写入安装目录：$target 被占用。请关闭 AI 社交秘书后重试。"
    }
    $targetSwapped = $true
    $installedExe = Join-Path $target $appExeName
    if (-not (Test-Path $installedExe)) { throw "安装后找不到 $installedExe" }

    # P0 修复：校验安装目录 exe 的 SHA-256 == payload-manifest.json 的 app_exe_sha256，
    # 防止"旧进程占用导致替换失败但脚本仍报告成功"的静默假成功。
    $manifestPath = Join-Path $scriptDir "payload-manifest.json"
    $expectedSha = $null
    if (Test-Path $manifestPath) {
        try {
            $manifest = Get-Content -Path $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($manifest.app_exe_sha256) { $expectedSha = ([string]$manifest.app_exe_sha256).ToLowerInvariant() }
        } catch { $expectedSha = $null }
    }
    if ($expectedSha) {
        $actualSha = (Get-FileHash -Path $installedExe -Algorithm SHA256).Hash.ToLowerInvariant()
        $result.expected_exe_sha256 = $expectedSha
        $result.installed_exe_sha256 = $actualSha
        if ($actualSha -ne $expectedSha) {
            throw "安装校验失败：安装目录 exe 的 SHA-256 与安装包清单不一致（可能被旧进程占用/安装包损坏）。已回滚，请确认 AI 社交秘书已关闭后重试。"
        }
        $result.sha256_verified = $true
        Write-Host "      已校验安装文件 SHA-256: $actualSha"
    } else {
        Write-Host "[校验] 安装包缺少 payload-manifest.json，跳过 SHA-256 校验" -ForegroundColor Yellow
    }

    # --- 3) 快捷方式 --------------------------------------------------------
    if (-not $SkipShortcuts) {
        Write-Host "[3/4] 创建快捷方式…"
        $shell = New-Object -ComObject WScript.Shell
        $programs = [Environment]::GetFolderPath("Programs")
        $startLnk = Join-Path $programs "$appName.lnk"
        $sc = $shell.CreateShortcut($startLnk)
        $sc.TargetPath = $installedExe
        $sc.WorkingDirectory = $target
        $sc.IconLocation = $installedExe
        $sc.Description = $appName
        $sc.Save()
        $createdShortcuts.Add($startLnk)
        $result.start_menu_lnk = $startLnk
        Write-Host "      开始菜单: $startLnk"

        if ($Desktop) {
            $desktopDir = [Environment]::GetFolderPath("Desktop")
            $deskLnk = Join-Path $desktopDir "$appName.lnk"
            $sc2 = $shell.CreateShortcut($deskLnk)
            $sc2.TargetPath = $installedExe
            $sc2.WorkingDirectory = $target
            $sc2.IconLocation = $installedExe
            $sc2.Description = $appName
            $sc2.Save()
            $createdShortcuts.Add($deskLnk)
            $result.desktop_lnk = $deskLnk
            Write-Host "      桌面: $deskLnk"
        }
    } else {
        Write-Host "[3/4] 已跳过快捷方式（-SkipShortcuts）"
    }

    # --- 4) HKCU 卸载登记项 -------------------------------------------------
    if (-not $SkipRegistry) {
        Write-Host "[4/4] 写入卸载登记项（HKCU，不需要管理员）…"
        if (Test-Path $uninstallKey) { Remove-Item -Path $uninstallKey -Recurse -Force -ErrorAction SilentlyContinue }
        New-Item -Path $uninstallKey -Force | Out-Null
        $estimatedKb = [int][math]::Max(1, [math]::Floor($bytes / 1024))
        $dataDir = Join-Path $env:LOCALAPPDATA "SocialSecretary"
        New-ItemProperty -Path $uninstallKey -Name "DisplayName" -Value $appName -PropertyType String -Force | Out-Null
        New-ItemProperty -Path $uninstallKey -Name "DisplayVersion" -Value $appVersion -PropertyType String -Force | Out-Null
        New-ItemProperty -Path $uninstallKey -Name "Publisher" -Value $publisher -PropertyType String -Force | Out-Null
        New-ItemProperty -Path $uninstallKey -Name "InstallLocation" -Value $target -PropertyType String -Force | Out-Null
        New-ItemProperty -Path $uninstallKey -Name "DisplayIcon" -Value $installedExe -PropertyType String -Force | Out-Null
        New-ItemProperty -Path $uninstallKey -Name "UninstallString" -Value ('"' + $installedExe + '" --uninstall --silent') -PropertyType String -Force | Out-Null
        New-ItemProperty -Path $uninstallKey -Name "QuietUninstallString" -Value ('"' + $installedExe + '" --uninstall --silent --no-confirm') -PropertyType String -Force | Out-Null
        New-ItemProperty -Path $uninstallKey -Name "EstimatedSize" -Value $estimatedKb -PropertyType DWord -Force | Out-Null
        New-ItemProperty -Path $uninstallKey -Name "NoModify" -Value 1 -PropertyType DWord -Force | Out-Null
        New-ItemProperty -Path $uninstallKey -Name "NoRepair" -Value 1 -PropertyType DWord -Force | Out-Null
        New-ItemProperty -Path $uninstallKey -Name "InstallDate" -Value (Get-Date -Format "yyyyMMdd") -PropertyType String -Force | Out-Null
        New-ItemProperty -Path $uninstallKey -Name "UserDataDir" -Value $dataDir -PropertyType String -Force | Out-Null
        $registryWritten = $true
        $result.registry_written = $true
        Write-Host "      登记项: $uninstallKey"
    } else {
        Write-Host "[4/4] 已跳过卸载登记项（-SkipRegistry）"
    }

    # --- 清理旧版本备份（覆盖安装成功后） -----------------------------------
    if ($backupMade -and (Test-Path $backup)) {
        if (-not (Remove-PathWithRetry -Path $backup -TimeoutSeconds 10)) {
            Write-Host "[提示] 旧版本目录备份未能删除（可手动删除）: $backup" -ForegroundColor Yellow
        }
    }

    # --- 完成 ---------------------------------------------------------------
    $result.ok = $true
    Write-Host ""
    Write-Host "安装完成！" -ForegroundColor Green
    Write-Host "  程序位置: $target"
    Write-Host "  卸载方式: 「设置 → 应用」找到「$appName」，或运行 卸载.cmd"
    if (-not $NoLaunch) {
        try {
            Start-Process -FilePath $installedExe -WorkingDirectory $target | Out-Null
            $result.launched = $true
            Write-Host "  已启动 $appName"
        } catch {
            Write-Host "  （启动失败，可手动打开开始菜单中的 $appName）" -ForegroundColor Yellow
        }
    }
    if (-not $Silent) {
        Write-Host ""
        Write-Host "按任意键关闭此窗口…"
        try { $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown") } catch { Start-Sleep -Seconds 3 }
    }
    Write-Result -ExitCode 0
} catch {
    $message = "$($_.Exception.Message)"
    Rollback
    Fail $message 3
}
