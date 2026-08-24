@echo off
setlocal
cd /d "%~dp0.."
rem Start the local admin process when it is not running yet.
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ok = Test-NetConnection 127.0.0.1 -Port 8080 -InformationLevel Quiet -WarningAction SilentlyContinue; if (-not $ok) { Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', (Join-Path (Get-Location) 'scripts\admin.cmd') -WindowStyle Hidden; for ($i = 0; $i -lt 30; $i++) { Start-Sleep -Seconds 1; if (Test-NetConnection 127.0.0.1 -Port 8080 -InformationLevel Quiet -WarningAction SilentlyContinue) { break } } }"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ok = Test-NetConnection 127.0.0.1 -Port 8081 -InformationLevel Quiet -WarningAction SilentlyContinue; if (-not $ok) { Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', (Join-Path (Get-Location) 'scripts\web.cmd') -WindowStyle Hidden; for ($i = 0; $i -lt 30; $i++) { Start-Sleep -Seconds 1; if (Test-NetConnection 127.0.0.1 -Port 8081 -InformationLevel Quiet -WarningAction SilentlyContinue) { break } } }"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_tunnel.ps1"
endlocal
