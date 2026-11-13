@echo off
where py >nul 2>nul || (echo Python was not found. Install Python 3 from https://www.python.org/downloads/ and tick "Add python.exe to PATH". & pause & exit /b 1)
start "" pyw "%~dp0fishflash_watcher.py" --calibrate
