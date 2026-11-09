# ============================================================================
# 一键构建 AI 社交秘书（PyInstaller onedir + 自研安装程序 + 便携 ZIP）
# ----------------------------------------------------------------------------
# 用法（在任意目录执行均可，脚本自动定位项目根）：
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Clean
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -SkipInstaller
#
# 安装器说明（2026-10-31 Lead 决策）：
# - 最终交付的安装器是 **自研 onefile 安装程序**（Tkinter，内嵌 onedir payload）：
#   dist\installer\AI社交秘书-0.2.5-安装包.zip
# - Inno Setup 版已作废：本机 Windows 11 25H2 (26200) 上 Inno 运行时无法初始化
#   （我们的旧安装器、最小 Inno 6.0.5 安装器、官方 innosetup-7.1.1 均 exit=1/挂起无日志，
#   full-access 下 HKCU 可写仍如此）。packaging/social_secretary.iss 仅作其他环境参考保留，
#   默认不再编译；如需编译可加 -LegacyInnoInstaller。
#
# 环境约束（见 AGENTS.md）：
# - 必须用工作区内的 .venv Python（已装 pyinstaller/Pillow）；
# - TMP/TEMP 指到工作区（沙箱只允许写 D:\测试项目-Agent）；
# - PYTHONPATH 加 src，保证 PyInstaller 能 import social_secretary；
# - 本脚本必须保存为 UTF-8 with BOM（Windows PowerShell 5.1 才能正确解析中文）；
# - 用 Invoke-NativeCommand 包装原生程序，避免 $ErrorActionPreference=Stop 把
#   原生程序写到 stderr 的正常日志当成终止错误。
#
# 产物：
#   dist\AI社交秘书\SocialSecretary.exe               PyInstaller onedir 目录版
#   dist\installer\AI社交秘书-0.2.5-安装包.zip       自研安装程序（最终交付）
#   dist\SocialSecretary-0.2.5-portable.zip           降级交付（免安装）
#   evidence\packaging\build_<ts>.json                构建证据
# ============================================================================
[CmdletBinding()]
param(
    [switch]$Clean,
    [switch]$SkipInstaller,
    [switch]$SkipZip,
    [switch]$SkipSelftest,
    [switch]$LegacyInnoInstaller,
    [string]$DistPath = "",
    [string]$WorkPath = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
$spec = Join-Path $root "packaging\social_secretary.spec"
$installerSpec = Join-Path $root "packaging\installer.spec"
$iss = Join-Path $root "packaging\social_secretary.iss"
$icon = Join-Path $root "packaging\assets\social_secretary.ico"
$iscc = Join-Path $root "tools\InnoSetup6\ISCC.exe"
$dist = if ($DistPath) { $DistPath } else { Join-Path $root "dist" }
$work = if ($WorkPath) { $WorkPath } else { Join-Path $root "build" }
$evidenceDir = Join-Path $root "evidence\packaging"
$appDist = Join-Path $dist "AI社交秘书"
$appExe = Join-Path $appDist "SocialSecretary.exe"
$installerOutDir = Join-Path $dist "installer"
$installerExe = Join-Path $installerOutDir "AI社交秘书-安装程序-0.2.5.exe"
$legacyInnoExe = Join-Path $installerOutDir "SocialSecretary-Setup-0.2.5.exe"
$ts = Get-Date -Format "yyyyMMdd_HHmmss"
$startedAt = (Get-Date).ToString("o")

# 工作区临时目录（沙箱只允许写 D:\测试项目-Agent 下；PyInstaller 也会写 TMP）
$env:TMP = Join-Path $root ".tmp"
$env:TEMP = $env:TMP
$env:UV_CACHE_DIR = Join-Path $root ".uv-cache"
$env:PYTHONPATH = Join-Path $root "src"
$env:PYTHONUTF8 = "1"
New-Item -ItemType Directory -Path $env:TMP -Force | Out-Null
New-Item -ItemType Directory -Path $evidenceDir -Force | Out-Null

$report = [ordered]@{
    started_at        = $startedAt
    project_root      = $root
    python            = $null
    pyinstaller       = $null
    spec              = $spec
    iss               = $iss
    iscc              = $iscc
    clean             = [bool]$Clean
    steps             = @()
    artifacts         = @()
    selftest          = $null
    ok                = $false
    errors            = @()
}

# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------
function Invoke-NativeCommand {
    <# 执行原生 exe 并合并 stdout+stderr。避免 $ErrorActionPreference=Stop 把
       原生程序 stderr 当成终止错误（Windows PowerShell 5.1 的已知坑）。 #>
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$ArgumentList = @(),
        [string]$LogPath = ""
    )
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $lines = New-Object System.Collections.Generic.List[string]
    $code = $null
    try {
        $raw = & $FilePath @ArgumentList 2>&1
        $code = $LASTEXITCODE
        foreach ($item in @($raw)) {
            if ($item -is [System.Management.Automation.ErrorRecord]) {
                $lines.Add($item.Exception.Message)
            } else {
                $lines.Add("$item")
            }
        }
    } catch {
        $lines.Add("$($_.Exception.Message)")
    } finally {
        $ErrorActionPreference = $prev
    }
    if ($LogPath) {
        $lines | Set-Content -Path $LogPath -Encoding UTF8
    }
    $arr = $lines.ToArray()
    return [pscustomobject]@{
        exit_code = $code
        lines     = $arr
        text      = ($arr -join "`n")
    }
}

function Add-Step {
    param([string]$Name, [string]$Status, [hashtable]$Detail = @{})
    $entry = [ordered]@{ name = $Name; status = $Status; at = (Get-Date).ToString("o") }
    foreach ($k in $Detail.Keys) { $entry[$k] = $Detail[$k] }
    $script:report.steps += $entry
}

function Invoke-SelftestProcess {
    <# 运行冻结版 exe 并捕获 stdout/stderr（默认 --selftest，可用 -ArgumentsOverride 自定义）。
       坑：PyInstaller windowed（console=False）的 GUI exe 在 PowerShell 的 & 调用下
       不会被等待，stdout 也无法直接捕获（实测输出为空、还可能导致后续文件被占用）。
       这里用 .NET Process + 重定向管道（已验证可拿到完整 JSON）。 #>
    param(
        [string]$Exe,
        [string]$DataDir = "",
        [string]$OutLog = "",
        [string[]]$ArgumentsOverride = @()
    )
    $res = [ordered]@{
        exe = $Exe; data_dir = $DataDir; exit_code = $null
        stdout = ""; stderr = ""; output = $null; ok = $false; error = $null
    }
    try {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $Exe
        if ($ArgumentsOverride -and $ArgumentsOverride.Count -gt 0) {
            $psi.Arguments = ($ArgumentsOverride -join " ")
        } else {
            $psi.Arguments = '--selftest --data-dir "' + $DataDir + '"'
        }
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
        $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8
        $proc = [System.Diagnostics.Process]::Start($psi)
        $stdout = $proc.StandardOutput.ReadToEnd()
        $stderr = $proc.StandardError.ReadToEnd()
        $proc.WaitForExit()
        $res.exit_code = $proc.ExitCode
        $res.stdout = $stdout.Trim()
        $res.stderr = $stderr.Trim()
        $res.output = $res.stdout
        if ($OutLog) { $res.stdout | Set-Content -Path $OutLog -Encoding UTF8 }
        try {
            $json = $res.stdout | ConvertFrom-Json
            $res.ok = [bool]$json.ok
            $res.version = $json.version
            $res.checks = @($json.checks | ForEach-Object { $_.name })
        } catch {
            # 非 --selftest 场景（如安装器 --selfcheck）不解析 stdout JSON 时不算错误
            if (-not $ArgumentsOverride) { $res.error = "JSON 解析失败: $($_.Exception.Message)" }
        }
    } catch {
        $res.error = $_.Exception.Message
    }
    return $res
}

function Get-Sha256 {
    param([string]$Path)
    if (Test-Path $Path -PathType Leaf) {
        return (Get-FileHash -Path $Path -Algorithm SHA256).Hash.ToLower()
    }
    return $null
}

function Get-DirStats {
    param([string]$Path)
    if (-not (Test-Path $Path)) { return $null }
    $files = @(Get-ChildItem -Path $Path -Recurse -File -ErrorAction SilentlyContinue)
    $sum = ($files | Measure-Object -Property Length -Sum).Sum
    if ($null -eq $sum) { $sum = 0 }
    return [ordered]@{ path = $Path; files = $files.Count; bytes = [int64]$sum; mib = [math]::Round($sum / 1MB, 2) }
}

Write-Host "== AI 社交秘书 构建开始 ==" -ForegroundColor Cyan
Write-Host "项目根: $root"
Write-Host "输出:   $dist"

# --- 0. 前置检查 -----------------------------------------------------------
if (-not (Test-Path $py)) {
    Write-Host "[fatal] 找不到项目 Python: $py" -ForegroundColor Red
    exit 2
}
if (-not (Test-Path $spec)) {
    Write-Host "[fatal] 找不到 spec: $spec" -ForegroundColor Red
    exit 2
}
$verRes = Invoke-NativeCommand -FilePath $py -ArgumentList @("-c", "import sys; print(sys.version.split()[0])")
$piRes = Invoke-NativeCommand -FilePath $py -ArgumentList @("-m", "PyInstaller", "--version")
$report.python = "$($verRes.text.Trim()) ($py)"
$report.pyinstaller = $piRes.text.Trim()
Write-Host "Python $($verRes.text.Trim()) / PyInstaller $($piRes.text.Trim())"

# 图标缺失时自动生成（Pillow 可用）
if (-not (Test-Path $icon)) {
    Write-Host "[icon] 图标不存在，调用 packaging\make_icon.py 生成"
    $iconRes = Invoke-NativeCommand -FilePath $py -ArgumentList @((Join-Path $root "packaging\make_icon.py"))
    if ($iconRes.exit_code -ne 0 -or -not (Test-Path $icon)) {
        Write-Host "[fatal] 图标生成失败: $($iconRes.text)" -ForegroundColor Red
        exit 2
    }
}

# --- 1. 清理 ---------------------------------------------------------------
if ($Clean) {
    foreach ($p in @($work, $appDist)) {
        if (Test-Path $p) {
            Write-Host "[clean] 删除 $p"
            Remove-Item -Path $p -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    # 安装器产物单独清理（保留 dist\installer 下其它历史证据文件）
    if (Test-Path $installerExe) {
        Remove-Item -Path $installerExe -Force -ErrorAction SilentlyContinue
    }
}

# --- 2. PyInstaller onedir 构建 -------------------------------------------
$piArgs = @("-m", "PyInstaller", "--noconfirm")
if ($Clean) { $piArgs += "--clean" }
$piArgs += @("--distpath", $dist, "--workpath", $work, $spec)

Write-Host "[build] pyinstaller ..." -ForegroundColor Yellow
$t0 = Get-Date
$piLog = Join-Path $env:TMP "pyinstaller_build.log"
$piRes = Invoke-NativeCommand -FilePath $py -ArgumentList $piArgs -LogPath $piLog
$piSeconds = [math]::Round(((Get-Date) - $t0).TotalSeconds, 1)
$piTail = (($piRes.lines | Select-Object -Last 40) -join "`n")
if ($piRes.exit_code -ne 0) {
    Add-Step -Name "pyinstaller" -Status "failed" -Detail @{ exit_code = $piRes.exit_code; seconds = $piSeconds; log = $piLog; tail = $piTail }
    $report.errors += "pyinstaller exit $($piRes.exit_code)"
    Write-Host "[build] PyInstaller 失败 (exit $($piRes.exit_code))，日志: $piLog" -ForegroundColor Red
    Write-Host $piTail
    $report.finished_at = (Get-Date).ToString("o")
    $report | ConvertTo-Json -Depth 8 | Set-Content -Path (Join-Path $evidenceDir "build_$ts.json") -Encoding UTF8
    exit 1
}
Add-Step -Name "pyinstaller" -Status "ok" -Detail @{ exit_code = 0; seconds = $piSeconds; log = $piLog }

if (-not (Test-Path $appExe)) {
    Write-Host "[fatal] 构建完成但找不到 $appExe" -ForegroundColor Red
    exit 3
}
$exeHash = Get-Sha256 $appExe
$exeSize = (Get-Item $appExe).Length

# --- 2b. 冻结模式下的运行时资源（exe 同目录） -------------------------------
# config.project_root() 在冻结模式 = exe 所在目录；QQ UIA 选择器配置必须出现在
# dist\AI社交秘书\config\qq_ui_profile.json（PyInstaller 6 的 datas 默认放 _internal\，
# 所以这里额外复制一份到 exe 同目录；安装包会把整个 dist 目录按原结构安装）。
$configSrc = Join-Path $root "config"
$runtimeDataCopied = @()
if (Test-Path $configSrc) {
    $configDst = Join-Path $appDist "config"
    New-Item -ItemType Directory -Path $configDst -Force | Out-Null
    Copy-Item -Path (Join-Path $configSrc "*") -Destination $configDst -Recurse -Force
    $runtimeDataCopied += "config/"
    Write-Host "[dist] 复制 config\ → $configDst（冻结模式 project_root() 读取）"
}
Add-Step -Name "runtime-datas" -Status "ok" -Detail @{ copied = ($runtimeDataCopied -join ", ") }

# --- 2c. 生成 onefile 安装器内嵌的 payload.zip（ZIP+LZMA 预压缩） -----------
# PyInstaller onefile 只做 zlib 级压缩；先压成 LZMA zip 可把安装器从 ~35MB 降到 ~20MB 量级。
$payloadZip = Join-Path $work "payload.zip"
if (-not (Test-Path $work)) { New-Item -ItemType Directory -Path $work -Force | Out-Null }
Write-Host "[payload] 生成 LZMA 压缩的 payload.zip ..." -ForegroundColor Yellow
$tPz = Get-Date
$payloadRes = Invoke-NativeCommand -FilePath $py -ArgumentList @(
    (Join-Path $root "packaging\make_payload_zip.py"),
    "--src", $appDist, "--dst", $payloadZip, "--level", "9"
)
$payloadSeconds = [math]::Round(((Get-Date) - $tPz).TotalSeconds, 1)
$payloadInfo = $null
try {
    $payloadInfo = ($payloadRes.text.Trim() | Select-Object -Last 1) | ConvertFrom-Json
} catch {
    $payloadInfo = $null
}
if ($payloadRes.exit_code -eq 0 -and (Test-Path $payloadZip)) {
    $payloadZipSize = (Get-Item $payloadZip).Length
    $payloadZipHash = Get-Sha256 $payloadZip
    $payloadRawBytes = 0
    $payloadFiles = 0
    if ($payloadInfo) {
        if ($payloadInfo.raw_bytes) { $payloadRawBytes = [int64]$payloadInfo.raw_bytes }
        if ($payloadInfo.files) { $payloadFiles = [int64]$payloadInfo.files }
    }
    $payloadRawMiB = [math]::Round($payloadRawBytes / 1MB, 2)
    $payloadZipMiB = [math]::Round($payloadZipSize / 1MB, 2)
    $payloadRatio = 0
    if ($payloadRawBytes -gt 0) { $payloadRatio = [math]::Round($payloadZipSize / $payloadRawBytes, 3) }
    Write-Host ("[payload] OK: {0} MiB → {1} MiB（ratio {2}）" -f $payloadRawMiB, $payloadZipMiB, $payloadRatio)
    $report.payload_zip = [ordered]@{
        path = $payloadZip
        bytes = $payloadZipSize
        mib = $payloadZipMiB
        sha256 = $payloadZipHash
        raw_bytes = $payloadRawBytes
        files = $payloadFiles
        seconds = $payloadSeconds
    }
    Add-Step -Name "payload-zip" -Status "ok" -Detail @{ bytes = $payloadZipSize; mib = $payloadZipMiB; seconds = $payloadSeconds }
} else {
    $report.errors += "payload.zip 生成失败 (exit $($payloadRes.exit_code))"
    Add-Step -Name "payload-zip" -Status "failed" -Detail @{ exit_code = $payloadRes.exit_code; tail = (($payloadRes.lines | Select-Object -Last 20) -join "`n") }
    Write-Host "[payload] 生成失败 (exit $($payloadRes.exit_code))" -ForegroundColor Red
    Write-Host (($payloadRes.lines | Select-Object -Last 20) -join "`n")
}

$distStats = Get-DirStats $appDist
Write-Host ("[build] OK: {0} ({1} bytes, sha256={2})" -f $appExe, $exeSize, $exeHash.Substring(0, 16))
Write-Host ("[build] dist 大小: {0} MiB / {1} 个文件" -f $distStats.mib, $distStats.files)
$report.runtime_data = $runtimeDataCopied
$report.artifacts += [ordered]@{ kind = "pyinstaller-onedir"; path = $appDist; exe = $appExe; exe_bytes = $exeSize; exe_sha256 = $exeHash; mib = $distStats.mib; files = $distStats.files }
Add-Step -Name "dist-check" -Status "ok" -Detail @{ exe_bytes = $exeSize; dist_mib = $distStats.mib }


# --- 3. 冻结版自检 --selftest ---------------------------------------------
if (-not $SkipSelftest) {
    $selftestData = Join-Path $env:TMP ("selftest_" + $ts)
    New-Item -ItemType Directory -Path $selftestData -Force | Out-Null
    Write-Host "[selftest] $appExe --selftest" -ForegroundColor Yellow
    $t0 = Get-Date
    $stRes = Invoke-SelftestProcess -Exe $appExe -DataDir $selftestData -OutLog (Join-Path $env:TMP "frozen_selftest_$ts.json")
    $stSeconds = [math]::Round(((Get-Date) - $t0).TotalSeconds, 1)
    $stText = $stRes.output
    $stOk = $stRes.ok
    $report.selftest = [ordered]@{
        ok = $stOk; exit_code = $stRes.exit_code; seconds = $stSeconds; data_dir = $selftestData
        output = $stText; error = $stRes.error; checks = $stRes.checks
    }
    if ($stOk -and $stRes.exit_code -eq 0) {
        Add-Step -Name "selftest" -Status "ok" -Detail @{ exit_code = 0; seconds = $stSeconds }
        Write-Host "[selftest] PASS (exit 0, ok=true)" -ForegroundColor Green
    } else {
        Add-Step -Name "selftest" -Status "failed" -Detail @{ exit_code = $stRes.exit_code; seconds = $stSeconds; output = $stText; error = $stRes.error }
        $report.errors += "selftest failed (exit $($stRes.exit_code), ok=$stOk, err=$($stRes.error))"
        Write-Host "[selftest] FAIL (exit $($stRes.exit_code), ok=$stOk)" -ForegroundColor Red
        Write-Host $stText
        Write-Host ("stderr: " + $stRes.stderr)
    }
} else {
    Add-Step -Name "selftest" -Status "skipped" -Detail @{ reason = "-SkipSelftest" }
}

# --- 4. 脚本安装器 ZIP（最终交付：ZIP + PowerShell 安装脚本，双击 安装.cmd） ----
# 为什么不用 Inno / PyInstaller onefile：
#   - 本机 Windows 11 25H2 (26200) 上 Inno Setup 运行时无法初始化（exit=1/挂起无日志，
#     full-access 下 HKCU 可写仍然失败）；
#   - PyInstaller onefile GUI 安装器在本机运行时解包失败（exit=-1，_MEI 解包被安全限制）。
# 因此最终交付为 ZIP + PowerShell 脚本安装器（完全受控/可读/可脚本验证，无需管理员权限）。
$appVersionForInstaller = "0.2.5"
$installerZip = Join-Path $installerOutDir ("AI社交秘书-" + $appVersionForInstaller + "-安装包.zip")
# 清理已废弃/旧版本安装器产物（Inno 版 / onefile 版 / v0.1.0 ZIP），避免误交付
# 清理旧版本安装包/便携包（保留当前版本；不按版本号硬编码，避免每次升版遗漏）
$currentVersionTag = $appVersionForInstaller
foreach ($old in @(Get-ChildItem -Path $installerOutDir -Filter "*安装包*.zip" -ErrorAction SilentlyContinue)) {
    if ($old.Name -notlike "*$currentVersionTag*") {
        Remove-Item -Path $old.FullName -Force -ErrorAction SilentlyContinue
        Write-Host "[installer] 已清理旧版本产物: $($old.FullName)" -ForegroundColor Yellow
    }
}
foreach ($old in @(Get-ChildItem -Path $dist -Filter "SocialSecretary-*-portable.zip" -ErrorAction SilentlyContinue)) {
    if ($old.Name -notlike "*$currentVersionTag*") {
        Remove-Item -Path $old.FullName -Force -ErrorAction SilentlyContinue
        Write-Host "[installer] 已清理旧版本便携包: $($old.FullName)" -ForegroundColor Yellow
    }
}
foreach ($old in @(Get-ChildItem -Path $installerOutDir -Filter "AI社交秘书-安装程序-*.exe" -ErrorAction SilentlyContinue)) {
    Remove-Item -Path $old.FullName -Force -ErrorAction SilentlyContinue
    Write-Host "[installer] 已清理废弃 onefile 安装器: $($old.FullName)" -ForegroundColor Yellow
}
foreach ($old in @(Get-ChildItem -Path $installerOutDir -Filter "SocialSecretary-Setup-*.exe" -ErrorAction SilentlyContinue)) {
    Remove-Item -Path $old.FullName -Force -ErrorAction SilentlyContinue
    Write-Host "[installer] 已清理废弃 Inno 安装器: $($old.FullName)" -ForegroundColor Yellow
}
foreach ($deprecated in @(
    $legacyInnoExe,
    (Join-Path $installerOutDir "AI社交秘书-安装程序-0.2.5.exe")
)) {
    if (Test-Path $deprecated) {
        Remove-Item -Path $deprecated -Force -ErrorAction SilentlyContinue
        Write-Host "[installer] 已删除废弃安装器: $deprecated" -ForegroundColor Yellow
    }
}
$obsoleteMarker = Join-Path $installerOutDir "_DEPRECATED_INSTALLERS.txt"
$obsoleteText = @(
    "已废弃的安装器方案说明（2026-10-31 / 2026-11-01）",
    "",
    "1) Inno Setup 方案（social_secretary.iss）：本机 Windows 11 25H2 (26200) 上",
    "   Inno 运行时无法初始化——我们编译的安装器 exit=1；最小 Inno 6.0.5 安装器挂起；",
    "   官方 innosetup-7.1.1 静默安装 exit=1；full-access 下 HKCU 可写仍失败且无日志。",
    "2) PyInstaller onefile 方案（installer.spec）：本机运行时解包失败（exit=-1，",
    "   _MEI 临时解包被安全限制），--selfcheck 也无法运行，故废弃。",
    "",
    "当前最终交付：AI社交秘书-0.2.5-安装包.zip（ZIP + PowerShell 脚本安装器）",
    "  解压后双击 安装.cmd；安装/卸载逻辑见 安装.ps1 / 卸载.ps1（可读、无需管理员）。",
    "  验证脚本：packaging/verify_install_custom.ps1",
    "",
    "历史上保留的参考文件：packaging/social_secretary.iss（仅其它环境参考）、",
    "packaging/installer.spec（已废弃）。"
) -join "`r`n"
Set-Content -Path $obsoleteMarker -Value $obsoleteText -Encoding UTF8

if (-not $SkipInstaller) {
    $zipMaker = Join-Path $root "packaging\make_installer_zip.py"
    if (-not (Test-Path $zipMaker)) {
        $report.errors += "缺少 make_installer_zip.py: $zipMaker"
        Add-Step -Name "script-installer-zip" -Status "failed" -Detail @{ reason = "make_installer_zip.py missing" }
    } else {
        Write-Host "[installer] 生成脚本安装器 ZIP（app/ onedir + 安装/卸载脚本）..." -ForegroundColor Yellow
        $t0 = Get-Date
        $zipRes = Invoke-NativeCommand -FilePath $py -ArgumentList @(
            $zipMaker, "--app", $appDist, "--out", $installerZip
        )
        $zipSeconds = [math]::Round(((Get-Date) - $t0).TotalSeconds, 1)
        $zipInfo = $null
        try {
            $zipInfo = ($zipRes.text.Trim() | Select-Object -Last 1) | ConvertFrom-Json
        } catch {
            $zipInfo = $null
        }
        if ($zipRes.exit_code -eq 0 -and (Test-Path $installerZip)) {
            $izSize = (Get-Item $installerZip).Length
            $izHash = Get-Sha256 $installerZip
            $izMiB = [math]::Round($izSize / 1MB, 2)
            Write-Host ("[installer] OK: {0} ({1} MiB, sha256={2})" -f $installerZip, $izMiB, $izHash.Substring(0, 16))
            $report.artifacts += [ordered]@{
                kind = "script-installer-zip"
                path = $installerZip
                bytes = $izSize
                mib = $izMiB
                sha256 = $izHash
                app_files = if ($zipInfo) { $zipInfo.app_files } else { 0 }
                app_bytes = if ($zipInfo) { $zipInfo.app_bytes } else { 0 }
            }
            Add-Step -Name "script-installer-zip" -Status "ok" -Detail @{
                exit_code = 0; seconds = $zipSeconds; path = $installerZip; bytes = $izSize
            }
            # 校验 ZIP 内容结构（安装脚本 + app 本体 + 说明）
            $verifyZip = Join-Path $env:TMP ("installer_zip_check_" + $ts)
            if (Test-Path $verifyZip) { Remove-Item -Path $verifyZip -Recurse -Force -ErrorAction SilentlyContinue }
            try {
                Expand-Archive -Path $installerZip -DestinationPath $verifyZip -Force
                $required = @("安装.cmd", "安装.ps1", "卸载.cmd", "卸载.ps1", "README-安装说明.txt",
                              "payload-manifest.json", "app\SocialSecretary.exe")
                $missing = @()
                foreach ($rel in $required) {
                    if (-not (Test-Path (Join-Path $verifyZip $rel))) { $missing += $rel }
                }
                if ($missing.Count -eq 0) {
                    Add-Step -Name "installer-zip-structure" -Status "ok" -Detail @{ required = $required.Count }
                    Write-Host "[installer] ZIP 结构校验通过（安装脚本 + app 本体齐全）" -ForegroundColor Green
                } else {
                    $report.errors += "安装器 ZIP 结构不完整: $($missing -join ', ')"
                    Add-Step -Name "installer-zip-structure" -Status "failed" -Detail @{ missing = ($missing -join ", ") }
                    Write-Host "[installer] ZIP 结构不完整: $($missing -join ', ')" -ForegroundColor Red
                }
            } catch {
                $report.errors += "安装器 ZIP 解压校验失败: $_"
                Add-Step -Name "installer-zip-structure" -Status "failed" -Detail @{ error = "$_" }
            } finally {
                Remove-Item -Path $verifyZip -Recurse -Force -ErrorAction SilentlyContinue
            }
        } else {
            $report.errors += "安装器 ZIP 生成失败 (exit $($zipRes.exit_code))"
            Add-Step -Name "script-installer-zip" -Status "failed" -Detail @{
                exit_code = $zipRes.exit_code; seconds = $zipSeconds
                tail = (($zipRes.lines | Select-Object -Last 20) -join "`n")
            }
            Write-Host "[installer] ZIP 生成失败 (exit $($zipRes.exit_code))" -ForegroundColor Red
            Write-Host (($zipRes.lines | Select-Object -Last 20) -join "`n")
        }
    }
}

# --- 4b. 历史参考：Inno Setup 安装包（默认不编译，仅 -LegacyInnoInstaller） ----
if ($LegacyInnoInstaller) {
    Write-Host "[legacy-inno] 编译 Inno Setup 安装包（仅供参考，本机运行时不可用）..." -ForegroundColor Yellow
    if (Test-Path $iscc) {
        $isccArgs = @("/Q", ("/O" + $installerOutDir), $iss)
        $isccRes = Invoke-NativeCommand -FilePath $iscc -ArgumentList $isccArgs
        $setupExe = $legacyInnoExe
        if ($isccRes.exit_code -eq 0 -and (Test-Path $setupExe)) {
            $report.artifacts += [ordered]@{ kind = "legacy-inno-setup (deprecated)"; path = $setupExe; bytes = (Get-Item $setupExe).Length }
            Add-Step -Name "legacy-inno" -Status "ok" -Detail @{ path = $setupExe; deprecated = $true }
        } else {
            $report.errors += "legacy Inno compile failed (exit $($isccRes.exit_code))"
            Add-Step -Name "legacy-inno" -Status "failed" -Detail @{ exit_code = $isccRes.exit_code; tail = (($isccRes.lines | Select-Object -Last 20) -join "`n") }
        }
    } else {
        Add-Step -Name "legacy-inno" -Status "skipped" -Detail @{ reason = "ISCC not found" }
    }
}

# --- 5. 便携 ZIP（降级交付：免安装、无需注册表） ---------------------------
if (-not $SkipZip) {
    $zipPath = Join-Path $dist "SocialSecretary-0.2.5-portable.zip"
    if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
    Write-Host "[zip] 打包 $zipPath" -ForegroundColor Yellow
    try {
        Compress-Archive -Path (Join-Path $appDist "*") -DestinationPath $zipPath -CompressionLevel Optimal
        $zipSize = (Get-Item $zipPath).Length
        $zipHash = Get-Sha256 $zipPath
        Write-Host ("[zip] OK: {0} ({1} MiB)" -f $zipPath, [math]::Round($zipSize / 1MB, 2))
        $report.artifacts += [ordered]@{ kind = "portable-zip"; path = $zipPath; bytes = $zipSize; mib = [math]::Round($zipSize / 1MB, 2); sha256 = $zipHash }
        Add-Step -Name "portable-zip" -Status "ok" -Detail @{ path = $zipPath; bytes = $zipSize }
    } catch {
        Add-Step -Name "portable-zip" -Status "failed" -Detail @{ error = "$_" }
        $report.errors += "zip failed: $_"
    }
}

# --- 6. 证据 --------------------------------------------------------------
$report.finished_at = (Get-Date).ToString("o")
$report.duration_seconds = [math]::Round(((Get-Date) - [datetime]::Parse($startedAt)).TotalSeconds, 1)
$report.ok = ($report.errors.Count -eq 0)
$evidencePath = Join-Path $evidenceDir "build_$ts.json"
$report | ConvertTo-Json -Depth 8 | Set-Content -Path $evidencePath -Encoding UTF8

Write-Host ""
Write-Host "== 构建汇总 ==" -ForegroundColor Cyan
foreach ($a in $report.artifacts) {
    Write-Host ("  {0,-22} {1}" -f $a.kind, $a.path)
}
Write-Host "  证据: $evidencePath"
if ($report.ok) {
    Write-Host "== 构建成功 ==" -ForegroundColor Green
    exit 0
} else {
    Write-Host "== 构建完成但有失败步骤: $($report.errors -join '; ') ==" -ForegroundColor Red
    exit 1
}
