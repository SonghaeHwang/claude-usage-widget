@echo off
rem 단일 exe 로 묶는다 -> dist\ClaudeUsageWidget.exe
cd /d "%~dp0"
uv run --with pyinstaller pyinstaller --noconfirm --clean --noconsole --onefile ^
  --name ClaudeUsageWidget ^
  --hidden-import pystray._win32 --hidden-import PIL._tkinter_finder ^
  widget.py
