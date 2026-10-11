# CI: auto-update test. Installs v0.0.1, serves a fake v0.0.2 from a local http.server, lets the RUNNING app upgrade itself,
# then serves a v0.0.3 with a tampered sha256 and checks it is rejected and 0.0.2 stays. Expects dist\YoussefConnector (PyInstaller
# output) and ISCC.exe. Never talks to GitHub: YC_UPDATE_URL points the app at the local server.
param([string]$Dist = "dist\YoussefConnector", [string]$Work = "ci-update")
$ErrorActionPreference = 'Stop'
$iscc = 'C:\Program Files (x86)\Inno Setup 6\ISCC.exe'
$work = Join-Path $PWD $Work
Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
$site = Join-Path $work 'site'; New-Item -ItemType Directory -Force $site | Out-Null
$appdir = Join-Path $env:LOCALAPPDATA 'Programs\YoussefConnector'
$home_ = Join-Path $work 'home'
$unkey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{2F1B6C0E-7A43-4B8D-9E15-C0DE5A7B3E92}_is1'

function Build-Setup($ver) {
    $src = Join-Path $work "dist-$ver"
    Copy-Item (Resolve-Path $Dist) $src -Recurse
    Get-ChildItem $src -Recurse -Filter VERSION | ForEach-Object { [IO.File]::WriteAllText($_.FullName, "$ver`n") }
    & $iscc "/DAppVersion=$ver" "/DSrcDir=$src" "/DOutDir=$work\installer" packaging\YoussefConnector.iss | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "ISCC $ver failed" }
    "$work\installer\YoussefConnector-Setup-$ver.exe"
}
function Publish($ver, $exe, [switch]$Tamper) {
    $name = "YoussefConnector-Setup-$ver.exe"
    Copy-Item $exe (Join-Path $site $name) -Force
    $h = (Get-FileHash -Algorithm SHA256 $exe).Hash.ToLower()
    if ($Tamper) { $h = '0' * 64 }
    "$h  $name" | Out-File -Encoding ascii (Join-Path $site "$name.sha256")
    $m = @{ version = $ver; url = "http://127.0.0.1:8765/$name" } | ConvertTo-Json -Compress
    [IO.File]::WriteAllText((Join-Path $site 'latest.json'), $m)
}
function Installed-Version() { (Get-ItemProperty $unkey -ErrorAction SilentlyContinue).DisplayVersion }
function Wait-Until($sb, $secs, $what) {
    $end = (Get-Date).AddSeconds($secs)
    while ((Get-Date) -lt $end) { if (& $sb) { return } ; Start-Sleep 1 }
    Get-Content (Join-Path $home_ 'connector.log') -ErrorAction SilentlyContinue | Select-Object -Last 40
    throw "timeout waiting for: $what"
}

$s1 = Build-Setup '0.0.1'
$s2 = Build-Setup '0.0.2'
$s3 = Build-Setup '0.0.3'
# the manifest starts at 0.0.1 = not newer, so the app that auto-starts after the silent install just idles
Publish '0.0.1' $s1
$srv = Start-Process python -ArgumentList '-m', 'http.server', '8765', '--bind', '127.0.0.1', '--directory', $site -PassThru -WindowStyle Hidden
Start-Sleep 2
# children (the installer, the app it starts) inherit these
$env:YC_HOME = $home_; $env:YC_UPDATE_URL = 'http://127.0.0.1:8765/latest.json'; $env:YC_UPDATE_EVERY = '4'; $env:YC_PORT = '0'
try {
    # WaitForExit, not -Wait: -Wait also waits for the app the installer starts
    $ip = Start-Process $s1 -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART' -PassThru; $ip.WaitForExit()
    Wait-Until { (Installed-Version) -eq '0.0.1' } 60 'v0.0.1 installed'
    Wait-Until { Get-Process YoussefConnector -ErrorAction SilentlyContinue } 30 'v0.0.1 running'
    Write-Host "OK installed 0.0.1 and running"

    Publish '0.0.2' $s2
    Wait-Until { (Installed-Version) -eq '0.0.2' } 120 'upgrade to 0.0.2 over the running app'
    Wait-Until { Get-Process YoussefConnector -ErrorAction SilentlyContinue } 60 'v0.0.2 running after the update'
    $v = (Get-ChildItem $appdir -Recurse -Filter VERSION | Select-Object -First 1 | Get-Content -Raw).Trim()
    if ($v -ne '0.0.2') { throw "VERSION file says $v after update" }
    Write-Host "OK upgraded to 0.0.2 over the running app"

    Publish '0.0.3' $s3 -Tamper
    Start-Sleep 20       # >= 4 update checks at YC_UPDATE_EVERY=4
    if ((Installed-Version) -ne '0.0.2') { throw "tampered update was installed: $(Installed-Version)" }
    if (-not (Get-Process YoussefConnector -ErrorAction SilentlyContinue)) { throw 'app not running after rejecting the tampered update' }
    $log = Get-Content (Join-Path $home_ 'connector.log') -Raw
    if ($log -notmatch 'sha256 mismatch') { throw 'no sha256 rejection logged' }
    Write-Host "OK tampered sha256 rejected, 0.0.2 stays"
} finally {
    Get-Process YoussefConnector -ErrorAction SilentlyContinue | Stop-Process -Force
    $un = Join-Path $appdir 'unins000.exe'
    if (Test-Path $un) { $up = Start-Process $un -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES' -PassThru; $up.WaitForExit() }
    Stop-Process -Id $srv.Id -Force -ErrorAction SilentlyContinue
}
