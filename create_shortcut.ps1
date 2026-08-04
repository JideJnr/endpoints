# Creates a desktop shortcut that launches PredictX backend + opens browser
$WshShell   = New-Object -ComObject WScript.Shell
$Desktop    = [System.Environment]::GetFolderPath("Desktop")
$Shortcut   = $WshShell.CreateShortcut("$Desktop\PredictX.lnk")

$Shortcut.TargetPath       = "C:\Users\Victor\Documents\Personal Workstation\football\predictx\launch_predictx.bat"
$Shortcut.WorkingDirectory = "C:\Users\Victor\Documents\Personal Workstation\football\predictx"
$Shortcut.Description      = "Start PredictX backend and open dashboard"
$Shortcut.WindowStyle      = 1   # 1 = normal window

# Use a football-related built-in icon (shell32 icon 43 = globe/network)
$Shortcut.IconLocation     = "shell32.dll,43"

$Shortcut.Save()
Write-Host "Desktop shortcut created: $Desktop\PredictX.lnk"
