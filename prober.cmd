@echo off
setlocal
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" (
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" "%~dp0prober.py" %*
) else (
    python "%~dp0prober.py" %*
)
exit /b %ERRORLEVEL%
