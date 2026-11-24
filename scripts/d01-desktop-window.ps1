# Native-window helper for the D-01-02 acceptance run (ASCII paths only).
# Actions: Info (JSON), Capture (native window PNG, including the OS title bar),
# Resize (real Win32 window resize and bring to front).
param(
  [Parameter(Mandatory=$true)][ValidateSet('Info','Capture','Resize','Close')][string]$Action,
  [Parameter(Mandatory=$true)][int]$ProcessId,
  [string]$OutFile,
  [int]$Width = 0,
  [int]$Height = 0
)
$ErrorActionPreference = 'Stop'
# Emit UTF-8 so the Chinese window title survives JSON parsing in the Node runner.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Drawing
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class D01Window {
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
  public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc cb, IntPtr p);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowTextW(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool MoveWindow(IntPtr h, int x, int y, int w, int ht, bool repaint);
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr dc, uint flags);
  [DllImport("user32.dll")] public static extern bool IsZoomed(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int w, int ht, uint flags);
  [DllImport("user32.dll")] public static extern bool PostMessageW(IntPtr h, uint msg, IntPtr w, IntPtr l);
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")] public static extern int GetSystemMetrics(int index);
}
'@
# Without DPI awareness a 150% desktop reports LOGICAL coordinates while
# CopyFromScreen uses PHYSICAL pixels, which crops the right/bottom of the window.
[void][D01Window]::SetProcessDPIAware()
$found = [IntPtr]::Zero
$script:fallback = [IntPtr]::Zero
$callback = [D01Window+EnumWindowsProc]{
  param([IntPtr]$hWnd, [IntPtr]$lParam)
  [uint32]$ownerPid = 0
  [void][D01Window]::GetWindowThreadProcessId($hWnd, [ref]$ownerPid)
  if ($ownerPid -eq $ProcessId -and [D01Window]::IsWindowVisible($hWnd)) {
    $sb = New-Object System.Text.StringBuilder 512
    [void][D01Window]::GetWindowTextW($hWnd, $sb, 512)
    # An undecorated window has no caption, so its title is empty. Prefer a titled
    # window, but fall back to the first visible window this process owns.
    if ($sb.Length -gt 0) { $script:found = $hWnd; return $false }
    if ($script:fallback -eq [IntPtr]::Zero) { $script:fallback = $hWnd }
  }
  return $true
}
[void][D01Window]::EnumWindows($callback, [IntPtr]::Zero)
if ($found -eq [IntPtr]::Zero -and $script:fallback -ne [IntPtr]::Zero) { $found = $script:fallback }
if ($found -eq [IntPtr]::Zero) { throw "No visible native window for pid $ProcessId" }
$rect = New-Object D01Window+RECT
[void][D01Window]::GetWindowRect($found, [ref]$rect)
$sb = New-Object System.Text.StringBuilder 512
[void][D01Window]::GetWindowTextW($found, $sb, 512)
$title = $sb.ToString()
$w = $rect.Right - $rect.Left
$h = $rect.Bottom - $rect.Top
if ($Action -eq 'Info') {
  [PSCustomObject]@{
    pid = $ProcessId; hwnd = [int64]$found; title = $title;
    left = $rect.Left; top = $rect.Top; width = $w; height = $h;
    screen_width = [D01Window]::GetSystemMetrics(0); screen_height = [D01Window]::GetSystemMetrics(1);
    # Report the window's own state as well: a maximised window covers the work
    # area (screen minus taskbar), so its height is legitimately smaller than the
    # screen and cannot be used to decide whether maximise worked.
    zoomed = [D01Window]::IsZoomed($found); iconic = [D01Window]::IsIconic($found);
    dpi_aware = $true;
    process = (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue).ProcessName
  } | ConvertTo-Json -Compress
  exit 0
}
[void][D01Window]::SetForegroundWindow($found)
if ($Action -eq 'Close') {
  # Same path a user takes: WM_CLOSE to the window, so the host runs its own exit
  # cleanup instead of being terminated from outside.
  [void][D01Window]::PostMessageW($found, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero)
  [PSCustomObject]@{ pid=$ProcessId; hwnd=[int64]$found; requested='WM_CLOSE' } | ConvertTo-Json -Compress
  exit 0
}
if ($Action -eq 'Resize') {
  if ($Width -le 0 -or $Height -le 0) { throw 'Resize needs -Width and -Height' }
  if (-not [D01Window]::MoveWindow($found, $rect.Left, $rect.Top, $Width, $Height, $true)) { throw 'MoveWindow failed' }
  Start-Sleep -Milliseconds 500
  $after = New-Object D01Window+RECT
  [void][D01Window]::GetWindowRect($found, [ref]$after)
  [PSCustomObject]@{ pid=$ProcessId; width=($after.Right-$after.Left); height=($after.Bottom-$after.Top) } | ConvertTo-Json -Compress
  exit 0
}
if (-not $OutFile) { throw 'Capture needs -OutFile' }
$parent = Split-Path -Parent $OutFile
if ($parent -and -not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
# Small settle time so the WebView has painted the latest React state.
Start-Sleep -Milliseconds 350
$rect2 = New-Object D01Window+RECT
[void][D01Window]::GetWindowRect($found, [ref]$rect2)
$w2 = $rect2.Right - $rect2.Left
$h2 = $rect2.Bottom - $rect2.Top
$bmp = New-Object System.Drawing.Bitmap($w2, $h2)
$gfx = [System.Drawing.Graphics]::FromImage($bmp)
# Prefer PrintWindow: it renders the window even if another app overlaps it.
$hdc = $gfx.GetHdc()
$printed = [D01Window]::PrintWindow($found, $hdc, 2)
$gfx.ReleaseHdc($hdc)
$center = $bmp.GetPixel([int]($w2/2), [int]($h2/2))
$blank = (-not $printed) -or ($center.R -lt 8 -and $center.G -lt 8 -and $center.B -lt 8)
if ($blank) {
  # Fallback: briefly make our own window topmost so the screen copy is not
  # covered by another application, then restore its normal z-order.
  $TOPMOST = [IntPtr](-1); $NOTOPMOST = [IntPtr](-2)
  $SWP_NOMOVE = 0x0002; $SWP_NOSIZE = 0x0001; $SWP_SHOWWINDOW = 0x0040
  [void][D01Window]::SetWindowPos($found, $TOPMOST, 0, 0, 0, 0, ($SWP_NOMOVE -bor $SWP_NOSIZE -bor $SWP_SHOWWINDOW))
  Start-Sleep -Milliseconds 250
  $src = New-Object System.Drawing.Point($rect2.Left, $rect2.Top)
  $dst = New-Object System.Drawing.Point(0, 0)
  $size = New-Object System.Drawing.Size($w2, $h2)
  $gfx.CopyFromScreen($src, $dst, $size)
  [void][D01Window]::SetWindowPos($found, $NOTOPMOST, 0, 0, 0, 0, ($SWP_NOMOVE -bor $SWP_NOSIZE))
}
$gfx.Dispose()
$bmp.Save($OutFile, [System.Drawing.Imaging.ImageFormat]::Png)
$bmp.Dispose()
[PSCustomObject]@{ path=$OutFile; width=$w2; height=$h2; title=$title } | ConvertTo-Json -Compress
