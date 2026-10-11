# Phone Link over UI Automation (runs in the owner's desktop session). Prints one JSON line.
#   -Action dial -Number 01xxxxxxxxx : tel: launch targeted at Phone Link (pre-fills its dialer), then invoke its Call button
#   -Action hangup                   : invoke an end-call button in any Phone Link window (none found -> ok=false)
#   -Action dump                     : visible Phone Link windows and their buttons/texts (to locate in-call controls)
param([ValidateSet('dial', 'hangup', 'dump')][string]$Action, [string]$Number = '')
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime, UIAutomationClient, UIAutomationTypes, System.Windows.Forms
Add-Type @"
using System; using System.Text; using System.Collections.Generic; using System.Runtime.InteropServices;
public static class PlWin { public delegate bool CB(IntPtr h, IntPtr l);
 [DllImport("user32.dll")] static extern bool EnumWindows(CB cb, IntPtr l);
 [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
 [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
 public static List<IntPtr> All() { var r = new List<IntPtr>(); EnumWindows((h, l) => { r.Add(h); return true; }, IntPtr.Zero); return r; } }
"@
$Scope = [Windows.Automation.TreeScope]; $AE = [Windows.Automation.AutomationElement]; $All = [Windows.Automation.Condition]::TrueCondition
$EndRe = '^(End call|End|Hang up|Hang Up|إنهاء المكالمة|إنهاء)$'

function Out-Json($o) { $o | ConvertTo-Json -Compress -Depth 4; exit 0 }
function PlWindows {
  $ids = @(Get-Process PhoneExperienceHost -ErrorAction SilentlyContinue | ForEach-Object Id)
  foreach ($h in [PlWin]::All()) {
    $p = 0; [void][PlWin]::GetWindowThreadProcessId($h, [ref]$p)
    if ($ids -contains $p -and [PlWin]::IsWindowVisible($h)) { try { $AE::FromHandle($h) } catch { } }
  }
}
function ById($root, $id) { $root.FindFirst($Scope::Descendants, (New-Object Windows.Automation.PropertyCondition($AE::AutomationIdProperty, $id))) }
$null = [Windows.ApplicationModel.Calls.PhoneCallManager, Windows.ApplicationModel.Calls, ContentType = WindowsRuntime]
function Wait-Call($secs) { for ($i = 0; $i -lt $secs * 4; $i++) { if ([Windows.ApplicationModel.Calls.PhoneCallManager]::IsCallActive) { return $true }; Start-Sleep -Milliseconds 250 }; $false }
function Invoke-El($e) { $e.GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern).Invoke() }

if (-not (Get-Process PhoneExperienceHost -ErrorAction SilentlyContinue)) { Out-Json @{ ok = $false; error = 'Phone Link is not running' } }

if ($Action -eq 'dial') {
  $digits = ($Number -replace '[^0-9+]', '')
  if ($digits.Length -lt 3) { Out-Json @{ ok = $false; error = 'bad number' } }
  $null = [Windows.System.Launcher, Windows.System, ContentType = WindowsRuntime]
  $asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | Select-Object -First 1
  $opt = New-Object Windows.System.LauncherOptions; $opt.TargetApplicationPackageFamilyName = 'Microsoft.YourPhone_8wekyb3d8bbwe'
  $launch = $asTask.MakeGenericMethod([bool]).Invoke($null, @([Windows.System.Launcher]::LaunchUriAsync([Uri]"tel:$digits", $opt)))
  [void]$launch.Wait(10000)
  $tail = $digits.Substring([Math]::Max(0, $digits.Length - 9))   # Phone Link may rewrite 01... as +20 1...
  for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Milliseconds 400
    foreach ($w in PlWindows) {
      $b = ById $w 'ButtonCall'
      if ($b -and (($b.Current.Name -replace '[^0-9]', '') -like "*$tail")) {
        # An offscreen Call button (short window) ignores Invoke: scroll it into view, invoke, and confirm that Windows
        # sees a call; else focus it and press Enter (verified 2026-10-10: Invoke alone did not dial, focus+Enter did).
        $name = $b.Current.Name
        try { $b.GetCurrentPattern([Windows.Automation.ScrollItemPattern]::Pattern).ScrollIntoView() } catch { }
        Invoke-El $b
        if (Wait-Call 5) { Out-Json @{ ok = $true; button = $name; method = 'invoke'; launched = $launch.Result } }
        try { $b.SetFocus(); [System.Windows.Forms.SendKeys]::SendWait('{ENTER}') } catch { }
        if (Wait-Call 6) { Out-Json @{ ok = $true; button = $name; method = 'enter'; launched = $launch.Result } }
        Out-Json @{ ok = $false; error = 'Phone Link did not start the call'; button = $name; launched = $launch.Result }
      }
    }
  }
  Out-Json @{ ok = $false; error = 'Phone Link dialer did not show the number'; launched = $launch.Result }
}

if ($Action -eq 'hangup') {
  foreach ($w in PlWindows) {
    foreach ($e in $w.FindAll($Scope::Descendants, $All)) {
      $c = $e.Current
      if ($c.ControlType.ProgrammaticName -eq 'ControlType.Button' -and ($c.Name -match $EndRe -or $c.AutomationId -match 'EndCall|HangUp|Hangup')) {
        Invoke-El $e
        Out-Json @{ ok = $true; button = $c.Name; id = $c.AutomationId; window = $w.Current.Name }
      }
    }
  }
  Out-Json @{ ok = $false; error = 'no end-call button in Phone Link' }
}

$out = @()
foreach ($w in PlWindows) {
  $ctl = @($w.FindAll($Scope::Descendants, $All) | ForEach-Object { $_.Current } |
    Where-Object { $_.ControlType.ProgrammaticName -match 'Button|Text' -and ($_.Name -or $_.AutomationId) -and $_.AutomationId -notmatch 'CallLog|^Button[0-9]$|PrimaryText' -and $_.Name -notmatch '^(Outgoing|Incoming|Missed)$' } |
    ForEach-Object { "$($_.ControlType.ProgrammaticName.Substring(12)) [$($_.Name)] $($_.AutomationId)" })
  $out += @{ window = $w.Current.Name; cls = $w.Current.ClassName; controls = $ctl }
}
Out-Json @{ ok = $true; windows = $out }
