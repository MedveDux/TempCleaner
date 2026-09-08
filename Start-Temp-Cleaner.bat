@echo off
cd /d "%~dp0"
where pythonw >nul 2>nul && (start "" pythonw "temp_cleaner.pyw" & exit /b)
where pyw     >nul 2>nul && (start "" pyw "temp_cleaner.pyw" & exit /b)
where python  >nul 2>nul && (start "" python "temp_cleaner.pyw" & exit /b)
echo Python not found. Install it from https://www.python.org/downloads/
echo IMPORTANT: tick "Add python.exe to PATH" during setup, then run this file again.
pause
