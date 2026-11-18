@echo off
rem 콘솔 창 없이 위젯 실행
cd /d "%~dp0"
start "" uv run pythonw widget.py
