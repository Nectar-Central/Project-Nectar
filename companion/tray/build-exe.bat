@echo off
REM Build tray exe — run from repo root
REM Bundles: decryptor.js + discord-presence.js (node fallback) + rich_presence.py
REM (in-process Python bridge) + pypresence so Discord IPC works inside the exe.
REM Requires Node.js + Python deps from companion\tray\requirements-tray.txt
if not exist "node_modules\discord-rpc" (
  echo discord-rpc missing — running npm install...
  call npm install
)
pip install -r companion\tray\requirements-tray.txt
pyinstaller --onefile --noconsole --name BuildNowTray --icon companion/nectar-presence.ico ^
  --version-file companion/tray/version_info.txt --noupx ^
  --paths companion ^
  --add-data "companion/decryptor.js;companion" ^
  --add-data "companion/discord-presence.js;companion" ^
  --add-data "companion/rich_presence.py;companion" ^
  --add-data "node_modules;node_modules" ^
  --add-data "companion/captures;companion/captures" ^
  --hidden-import=rich_presence ^
  --hidden-import=pypresence --hidden-import=pypresence.presence --hidden-import=pypresence.payloads --hidden-import=pypresence.types --hidden-import=pypresence.utils ^
  --hidden-import=pystray --hidden-import=PIL --hidden-import=requests ^
  companion/tray/tray_app.py
echo Built dist/BuildNowTray.exe — runs headless in tray.
echo If 8765 still fails, check %USERPROFILE%\.buildnow-tray.log
