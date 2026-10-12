# youssef-repair-version: 1 #
# Youssef Connector Bluetooth repair. Installed by `YoussefConnector.exe --install-repair` (elevated, once) as
# %ProgramFiles%\YoussefConnector\repair\repair.ps1 and run ONLY by the SYSTEM scheduled task "YoussefRepair" (no arguments;
# the app starts it with `schtasks /run /tn YoussefRepair`). Steps: restart every present USB/PCI Bluetooth adapter
# (pnputil /restart-device, fallback Disable/Enable-PnpDevice), restart the services bthserv and BTAGService, then write
# last.json {time, epoch, steps, rc, ...} next to this script (Users can read it through the folder ACL).
# rc: 0 all steps ok, 1 a step failed, 2 no Bluetooth adapter found.
# YOUSSEF_REPAIR_FAKE=1 (CI only, has no effect on the SYSTEM task, which never sees that variable): discovery runs, nothing is
# restarted, and YOUSSEF_REPAIR_OUT may redirect last.json.
$ErrorActionPreference = 'Continue'
$fake = $env:YOUSSEF_REPAIR_FAKE -eq '1'
$outDir = $PSScriptRoot
if ($fake -and $env:YOUSSEF_REPAIR_OUT) { $outDir = $env:YOUSSEF_REPAIR_OUT }
$steps = New-Object System.Collections.ArrayList
$script:rc = 0
$t0 = Get-Date
$pnputil = [IO.Path]::Combine([Environment]::SystemDirectory, 'pnputil.exe')   # absolute: no PATH lookup as SYSTEM

function Add-Step([string]$name, [bool]$ok, [string]$detail) {
    [void]$steps.Add([ordered]@{ step = $name; ok = $ok; detail = $detail })
    if (-not $ok -and $script:rc -eq 0) { $script:rc = 1 }
}

function Restart-Adapter([string]$id) {
    if ($fake) { Add-Step 'restart_device' $true "fake: skipped $id"; return }
    $out = & $pnputil /restart-device "$id" 2>&1 | Out-String
    $code = $LASTEXITCODE
    if ($code -eq 0 -or $code -eq 3010) {
        Add-Step 'restart_device' $true "pnputil rc=$code $id"
        return
    }
    # older Windows without /restart-device, or the restart was refused: disable + enable
    try {
        Disable-PnpDevice -InstanceId $id -Confirm:$false -ErrorAction Stop
        Start-Sleep -Seconds 2
        Enable-PnpDevice -InstanceId $id -Confirm:$false -ErrorAction Stop
        Add-Step 'restart_device' $true "pnputil rc=$code, disable/enable ok $id"
    } catch {
        # make sure a half-done fallback never leaves the adapter disabled
        try { Enable-PnpDevice -InstanceId $id -Confirm:$false -ErrorAction Stop } catch { }
        Add-Step 'restart_device' $false ("pnputil rc=$code " + $out.Trim() + "; disable/enable: " + $_.Exception.Message + " $id")
    }
}

function Restart-Svc([string]$name) {
    $svc = Get-Service -Name $name -ErrorAction SilentlyContinue
    if (-not $svc) { Add-Step "restart_$name" $true 'service not present'; return }
    if ($fake) { Add-Step "restart_$name" $true "fake: skipped ($($svc.Status))"; return }
    try {
        Restart-Service -Name $name -Force -ErrorAction Stop
        Add-Step "restart_$name" $true ((Get-Service -Name $name).Status.ToString())
    } catch {
        Add-Step "restart_$name" $false $_.Exception.Message
    }
}

$ids = @()
try {
    try {
        $ids = @(Get-PnpDevice -Class Bluetooth -PresentOnly -ErrorAction Stop |
                 Where-Object { $_.InstanceId -match '^(USB|PCI)\\' } | ForEach-Object { $_.InstanceId })
        Add-Step 'find_adapters' ($ids.Count -gt 0) ("found " + $ids.Count)
        if ($ids.Count -eq 0) { $script:rc = 2 }
    } catch {
        # Get-PnpDevice throws when no device of the class exists
        Add-Step 'find_adapters' $false $_.Exception.Message
        $script:rc = 2
    }
    foreach ($id in $ids) { Restart-Adapter $id }
    if ($ids.Count -gt 0 -and -not $fake) { Start-Sleep -Seconds 3 }   # let the radio enumerate before the services come back
    Restart-Svc 'bthserv'
    Restart-Svc 'BTAGService'
} catch {
    Add-Step 'unexpected' $false $_.Exception.Message
} finally {
    $now = Get-Date
    $user = ''
    try { $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name } catch { }
    $doc = [ordered]@{
        time     = $now.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss.fffZ')
        epoch    = [math]::Round(($now.ToUniversalTime() - [datetime]'1970-01-01').TotalSeconds, 3)
        rc       = $script:rc
        steps    = @($steps)
        adapters = @($ids)
        seconds  = [math]::Round(($now - $t0).TotalSeconds, 1)
        user     = $user
        fake     = $fake
        version  = 1
    }
    $json = ConvertTo-Json -InputObject $doc -Depth 5
    $dest = Join-Path $outDir 'last.json'
    $tmp = Join-Path $outDir 'last.json.tmp'
    # write then rename: the app re-reads last.json on every mtime change and must never see half a file
    [IO.File]::WriteAllText($tmp, $json, (New-Object System.Text.UTF8Encoding $false))
    if (Test-Path -LiteralPath $dest) { [IO.File]::Replace($tmp, $dest, [NullString]::Value) } else { [IO.File]::Move($tmp, $dest) }
}
exit $script:rc
