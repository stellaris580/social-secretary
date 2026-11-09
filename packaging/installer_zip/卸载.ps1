# ============================================================================
# AI 社交秘书 卸载脚本（脚本安装器配套，无需第三方卸载器）
# ----------------------------------------------------------------------------
# 双击入口：卸载.cmd（调用本脚本）。命令行用法：
#   卸载.ps1                                   交互卸载（询问是否删除本地数据，默认保留）
#   卸载.ps1 -Silent -DeleteData               静默卸载并删除本地数据
#   卸载.ps1 -Silent                           静默卸载（保留本地数据）
#   卸载.ps1 -InstallDir <路径> -DataDir <路径> -ResultPath <json>
#
# 卸载流程：
#   1) 优先调用已安装程序自带的 `SocialSecretary.exe --uninstall --silent`（app.py 转发到
#      installer.wizard；由它删除快捷方式、HKCU 卸载登记项，并调度程序目录延迟自删除）；
#   2) 已安装程序不存在时降级为脚本手工清理（快捷方式/登记项/目录/可选数据）；
#   3) 用户数据目录 %LOCALAPPDATA%\SocialSecretary 默认保留，只有用户明确选择或
#      -DeleteData 时才删除（不可恢复）；
#   4) 输出结果 JSON（stdout + -ResultPath 文件）。
#
# 安全：不需要管理员权限；只删除本程序自己的目录/快捷方式/登记项/数据目录。
# 本文件必须保存为 UTF-8 with BOM（Windows PowerShell 5.1 才能正确解析中文）。
# ============================================================================
[CmdletBinding()]
param(
    [switch]$Silent,
    [switch]$DeleteData,
    [string]$InstallDir = "",
    [string]$DataDir = "",
    [string]$ResultPath = ""
)

$ErrorActionPreference = "Stop"
$appName = "AI 社交秘书"
$appExeName = "SocialSecretary.exe"
$uninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\SocialSecretary"

$result = [ordered]@{
    mode = "uninstall"
    ok = $false
    exit_code = 1
    install_dir = $null
    data_dir = $null
    delete_data = [bool]$DeleteData
    method = $null
    app_dir_removed = $false
    start_menu_lnk_removed = $false
    desktop_lnk_removed = $false
    registry_removed = $false
    data_deleted = $false
    data_preserved = $false
    fallback_cleanup = @()
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

function Invoke-ExeCapture {
    <# 运行（可能无控制台的）程序并等待退出，捕获 stdout/stderr。 #>
    param([string]$Exe, [string]$Arguments = "", [int]$TimeoutMs = 300000)
    $res = [ordered]@{ exit_code = $null; stdout = ""; stderr = ""; error = $null }
    try {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $Exe
        $psi.Arguments = $Arguments
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
        $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8
        $proc = [System.Diagnostics.Process]::Start($psi)
        $stdout = $proc.StandardOutput.ReadToEnd()
        $stderr = $proc.StandardError.ReadToEnd()
        if (-not $proc.WaitForExit($TimeoutMs)) { try { $proc.Kill() } catch { } }
        $res.exit_code = $proc.ExitCode
        $res.stdout = $stdout.Trim()
        $res.stderr = $stderr.Trim()
    } catch {
        $res.error = $_.Exception.Message
    }
    return $res
}

function Wait-PathGone {
    param([string]$Path, [int]$TimeoutSeconds = 90)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (-not (Test-Path $Path)) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return -not (Test-Path $Path)
}

function Resolve-InstallDir {
    if ($InstallDir) { return [System.IO.Path]::GetFullPath($InstallDir) }
    if (Test-Path $uninstallKey) {
        try {
            $props = Get-ItemProperty -Path $uninstallKey -ErrorAction SilentlyContinue
            if ($props -and $props.InstallLocation) { return [System.IO.Path]::GetFullPath([string]$props.InstallLocation) }
        } catch { }
    }
    return (Join-Path $env:LOCALAPPDATA "Programs\SocialSecretary")
}

function Resolve-DataDir {
    if ($DataDir) { return [System.IO.Path]::GetFullPath($DataDir) }
    if (Test-Path $uninstallKey) {
        try {
            $props = Get-ItemProperty -Path $uninstallKey -ErrorAction SilentlyContinue
            if ($props -and $props.UserDataDir) { return [System.IO.Path]::GetFullPath([string]$props.UserDataDir) }
        } catch { }
    }
    return (Join-Path $env:LOCALAPPDATA "SocialSecretary")
}

Write-Host "== $appName 卸载程序 ==" -ForegroundColor Cyan
$target = Resolve-InstallDir
$dataTarget = Resolve-DataDir
$result.install_dir = $target
$result.data_dir = $dataTarget
Write-Host "程序目录: $target"
Write-Host "数据目录: $dataTarget"

if (-not $Silent) {
    $answer = Read-Host "确定要卸载 $appName 吗？(y/N)"
    if ($answer -notmatch "^(y|yes|是)$") { Write-Host "已取消"; Write-Result -ExitCode 1 }
    $deleteAnswer = Read-Host "是否同时删除本地消息/摘要/设置数据（不可恢复，默认保留）？(y/N)"
    if ($deleteAnswer -match "^(y|yes|是)$") { $DeleteData = $true }
    $result.delete_data = [bool]$DeleteData
}

$installedExe = Join-Path $target $appExeName
$installResultFile = Join-Path $env:TEMP ("ss_uninstall_" + (Get-Date -Format "yyyyMMdd_HHmmss") + ".json")
$appDirRemoved = $false

try {
    if (Test-Path $installedExe) {
        # --- 首选：调用程序自带卸载（删除快捷方式/登记项 + 延迟自删除程序目录） ---
        $result.method = "installed-exe"
        Write-Host "[1/2] 调用 $installedExe --uninstall --silent …"
        $args = @("--uninstall", "--silent", "--no-confirm",
                  ('--dir="' + $target + '"'), ('--data-dir="' + $dataTarget + '"'),
                  ('--result="' + $installResultFile + '"'))
        if ($DeleteData) { $args += "--delete-data=1" }
        $run = Invoke-ExeCapture -Exe $installedExe -Arguments ($args -join " ") -TimeoutMs 300000
        $result.app_uninstall_exit_code = $run.exit_code
        $result.app_uninstall_stdout = $run.stdout
        $appDirRemoved = Wait-PathGone -Path $target -TimeoutSeconds 90

        # --- 兜底清理：安装器卸载若因只读/占用/权限策略没删干净，脚本再直接清一遍 ---
        $programsDir = [Environment]::GetFolderPath("Programs")
        $desktopPath = [Environment]::GetFolderPath("Desktop")
        $fallbackActions = @()
        foreach ($lnk in @((Join-Path $programsDir "$appName.lnk"), (Join-Path $desktopPath "$appName.lnk"))) {
            if (-not (Test-Path $lnk)) { continue }
            $fallbackActions += "shortcut:$lnk"
            for ($i = 0; $i -lt 3; $i++) {
                try {
                    attrib -R "$lnk" 2>$null | Out-Null
                    Remove-Item -Path $lnk -Force -ErrorAction Stop
                    break
                } catch { Start-Sleep -Milliseconds 250 }
            }
        }
        if (Test-Path $uninstallKey) {
            $fallbackActions += "registry:$uninstallKey"
            try { reg.exe delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\SocialSecretary" /f 2>$null | Out-Null } catch { }
            for ($i = 0; $i -lt 3 -and (Test-Path $uninstallKey); $i++) {
                try { Remove-Item -Path $uninstallKey -Recurse -Force -ErrorAction SilentlyContinue } catch { }
                Start-Sleep -Milliseconds 250
            }
        }
        if (-not $appDirRemoved -and (Test-Path $target)) {
            $fallbackActions += "appdir:$target"
            for ($i = 0; $i -lt 5 -and (Test-Path $target); $i++) {
                try { attrib -R "$target\*" /S /D 2>$null | Out-Null } catch { }
                try { Remove-Item -Path $target -Recurse -Force -ErrorAction SilentlyContinue } catch { }
                Start-Sleep -Milliseconds 500
            }
            $appDirRemoved = -not (Test-Path $target)
        }
        $result.fallback_cleanup = $fallbackActions
    } else {
        # --- 降级：脚本手工清理 ---
        $result.method = "script-fallback"
        Write-Host "[1/2] 找不到已安装程序，改为脚本清理…"
        $programs = [Environment]::GetFolderPath("Programs")
        $desktopDir = [Environment]::GetFolderPath("Desktop")
        foreach ($lnk in @((Join-Path $programs "$appName.lnk"), (Join-Path $desktopDir "$appName.lnk"))) {
            try { if (Test-Path $lnk) { Remove-Item -Path $lnk -Force -ErrorAction SilentlyContinue } } catch { }
        }
        if (Test-Path $uninstallKey) {
            try { Remove-Item -Path $uninstallKey -Recurse -Force -ErrorAction SilentlyContinue } catch { }
        }
        if (Test-Path $target) {
            try { Remove-Item -Path $target -Recurse -Force -ErrorAction SilentlyContinue } catch { }
        }
        $appDirRemoved = -not (Test-Path $target)
    }

    # --- 数据目录（默认保留，只有明确选择才删除） --------------------------
    Write-Host "[2/2] 处理用户数据…"
    if ($DeleteData) {
        if (Test-Path $dataTarget) { Remove-Item -Path $dataTarget -Recurse -Force -ErrorAction SilentlyContinue }
        $result.data_deleted = -not (Test-Path $dataTarget)
        $result.data_preserved = -not $result.data_deleted
        Write-Host "      本地数据已删除: $dataTarget"
    } else {
        $result.data_preserved = Test-Path $dataTarget
        Write-Host "      本地数据已保留: $dataTarget（可稍后手动删除）"
    }

    # --- 最终状态断言 -------------------------------------------------------
    $programsDir = [Environment]::GetFolderPath("Programs")
    $desktopPath = [Environment]::GetFolderPath("Desktop")
    $result.app_dir_removed = $appDirRemoved
    $result.start_menu_lnk_removed = -not (Test-Path (Join-Path $programsDir "$appName.lnk"))
    $result.desktop_lnk_removed = -not (Test-Path (Join-Path $desktopPath "$appName.lnk"))
    $result.registry_removed = -not (Test-Path $uninstallKey)

    $result.ok = $appDirRemoved -and $result.start_menu_lnk_removed -and $result.desktop_lnk_removed -and $result.registry_removed
    if ($result.ok) {
        Write-Host ""
        Write-Host "卸载完成！" -ForegroundColor Green
        if ($result.data_preserved) { Write-Host "  本地数据已保留（如需删除，请手动删除 $dataTarget）" }
        if (-not $Silent) {
            Write-Host "按任意键关闭此窗口…"
            try { $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown") } catch { Start-Sleep -Seconds 3 }
        }
        Write-Result -ExitCode 0
    } else {
        $result.error = "卸载后仍有残留（见 app_dir_removed / lnk_removed / registry_removed 字段）"
        Write-Host "[警告] $($result.error)" -ForegroundColor Yellow
        Write-Result -ExitCode 3
    }
} catch {
    $result.error = "$($_.Exception.Message)"
    Write-Host "[失败] $($result.error)" -ForegroundColor Red
    Write-Result -ExitCode 3
}
