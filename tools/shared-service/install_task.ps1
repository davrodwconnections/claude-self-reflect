$ErrorActionPreference='Stop'
$base='C:\Users\davro\.local\share\csr-shared-service'
# The copied cache is isolated from legacy clients; rebuild it from canonical SQLite once.
$cache=[IO.Path]::GetFullPath('C:\Users\davro\.claude-self-reflect\shared-index')
$destination=[IO.Path]::GetFullPath((Join-Path $base 'backups\before-shared-service\seeded-index'))
if ($cache -ne 'C:\Users\davro\.claude-self-reflect\shared-index' -or -not $destination.StartsWith($base+'\')) { throw 'Unsafe cache move target' }
if (Test-Path -LiteralPath $cache) { Move-Item -LiteralPath $cache -Destination $destination }
$user=[System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$pythonw=Join-Path $base '.venv\Scripts\pythonw.exe'
$action=New-ScheduledTaskAction -Execute $pythonw -Argument ('"'+(Join-Path $base 'service.py')+'"') -WorkingDirectory $base
$login=New-ScheduledTaskTrigger -AtLogOn -User $user
$principal=New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings=New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -Hidden -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName 'CSR-Shared-Service' -Action $action -Trigger $login -Principal $principal -Settings $settings -Description 'Local authenticated shared CSR; one lazy worker, idle unload, no paid narration.' | Out-Null
Start-ScheduledTask -TaskName 'CSR-Shared-Service'
Export-ScheduledTask -TaskName 'CSR-Shared-Service' | Set-Content -LiteralPath (Join-Path $base 'scheduled-task.xml') -Encoding utf8
