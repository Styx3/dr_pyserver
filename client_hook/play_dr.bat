@echo off
rem ---------------------------------------------------------------------------
rem play_dr.bat — launch DungeonRunners.exe directly with the HP-synch hook.
rem
rem Place this file (and the proxy d3d9.dll) IN the game folder, then run it.
rem It cd's to its own folder (so the client finds LauncherID.ini / .cfg / content)
rem and starts the client with the ran_from_launcher token the NCLauncher passes.
rem The proxy d3d9.dll in the same folder is auto-loaded by the client and
rem installs the FUN_005dd900 bypass — no drloader.exe / injection needed.
rem (d3d9.dll's launcher-token hook also makes a bare DungeonRunners.exe launch
rem work, so this .bat is just a convenience.)
rem
rem Pass-through args are forwarded (e.g.  play_dr.bat /authserver=127.0.0.1:2110 ).
rem ---------------------------------------------------------------------------
cd /d "%~dp0"
start "" "DungeonRunners.exe" ran_from_launcher %*
