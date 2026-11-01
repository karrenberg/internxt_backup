@echo off
rem Resolve the script beside this launcher so it works from any source directory.
python "%~dp0internxtcp.py" %*
exit /b %errorlevel%
