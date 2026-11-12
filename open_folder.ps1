param([Parameter(Mandatory=$true)][string]$Folder)
$ErrorActionPreference = 'Stop'
try {
    $directory = Get-Item -LiteralPath $Folder -ErrorAction Stop
    if (-not $directory.PSIsContainer) { throw 'Target must be a directory' }
    $targetPath = $directory.FullName.TrimEnd('\')
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class FolderWindow {
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint p);
    [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
    [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr h, int command);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr h);
    [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint a, uint b, bool attach);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
    public static bool Activate(IntPtr h) {
        if (IsIconic(h)) ShowWindowAsync(h, 9);
        SetForegroundWindow(h);
        if (GetForegroundWindow() == h) return true;
        uint ignored;
        uint current = GetCurrentThreadId();
        uint foreground = GetWindowThreadProcessId(GetForegroundWindow(), out ignored);
        uint target = GetWindowThreadProcessId(h, out ignored);
        bool attachedForeground = foreground != 0 && foreground != current && AttachThreadInput(current, foreground, true);
        bool attachedTarget = target != 0 && target != current && target != foreground && AttachThreadInput(current, target, true);
        try { BringWindowToTop(h); SetForegroundWindow(h); }
        finally {
            if (attachedTarget) AttachThreadInput(current, target, false);
            if (attachedForeground) AttachThreadInput(current, foreground, false);
        }
        return GetForegroundWindow() == h;
    }
}
'@
    $shell = New-Object -ComObject Shell.Application
    $shell.Open($directory.FullName)
    $deadline = [DateTime]::UtcNow.AddSeconds(4)
    do {
        foreach ($window in @($shell.Windows())) {
            try {
                $path = [string]$window.Document.Folder.Self.Path
                if ($path.TrimEnd('\').Equals($targetPath, [StringComparison]::OrdinalIgnoreCase)) {
                    $handle = [IntPtr][long]$window.HWND
                    if ([FolderWindow]::Activate($handle)) {
                        Write-Output ('FOREGROUND_OK hwnd=' + $handle.ToInt64())
                        exit 0
                    }
                }
            } catch { continue }
        }
        Start-Sleep -Milliseconds 100
    } while ([DateTime]::UtcNow -lt $deadline)
    Write-Error 'Folder opened, but Windows did not activate its Explorer window.'
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
