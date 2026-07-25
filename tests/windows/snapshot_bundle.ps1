param(
    [Parameter(Mandatory = $true)]
    [string]$BundleRoot
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path -LiteralPath $BundleRoot).Path
$launcher = Join-Path $root "TextSnapLayout.exe"
if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
    throw "BundleRoot does not contain TextSnapLayout.exe"
}

function Get-BundleSnapshot {
    param([string]$SnapshotRoot)

    $entries = Get-ChildItem -LiteralPath $SnapshotRoot -Force -Recurse |
        Sort-Object FullName
    $snapshot = @{}
    foreach ($entry in $entries) {
        $relative = [IO.Path]::GetRelativePath($SnapshotRoot, $entry.FullName)
        if ($entry.PSIsContainer) {
            $snapshot[$relative] = "directory"
        } else {
            $hash = (Get-FileHash -LiteralPath $entry.FullName -Algorithm SHA256).Hash
            $snapshot[$relative] = "file:$($entry.Length):$hash"
        }
    }
    return $snapshot
}

Write-Host "正在计算运行前快照（大型便携包可能需要数分钟）..."
$before = Get-BundleSnapshot -SnapshotRoot $root
Start-Process -FilePath $launcher

Write-Host ""
Write-Host "请完成离线 OCR、取消、复制、设置保存和退出测试。"
Read-Host "确认 TextSnap Layout 已从托盘正常退出后按 Enter"

$runtimePython = Join-Path $root "runtime\pythonw.exe"
$running = Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" |
    Where-Object { $_.ExecutablePath -eq $runtimePython }
if ($running) {
    throw "TextSnap Layout is still running; exit it normally before comparison"
}

Write-Host "正在计算运行后快照..."
$after = Get-BundleSnapshot -SnapshotRoot $root
$allowed = [IO.Path]::Combine("data", "settings.json")
$changes = @()
$allPaths = @($before.Keys + $after.Keys) | Sort-Object -Unique
foreach ($relative in $allPaths) {
    if ($relative -eq $allowed) {
        continue
    }
    $beforeValue = $before[$relative]
    $afterValue = $after[$relative]
    if ($beforeValue -ne $afterValue) {
        $changes += [PSCustomObject]@{
            Path = $relative
            Before = $beforeValue
            After = $afterValue
        }
    }
}

if ($changes.Count -ne 0) {
    $changes | Format-Table -AutoSize
    throw "Unexpected bundle changes were detected"
}

Write-Host "通过：除 data/settings.json 外，程序目录内容未改变。"
