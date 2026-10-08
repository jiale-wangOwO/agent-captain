@echo off
rem Backward-compatible launcher; state and running worker identities stay unchanged.
call "%~dp0run_pron.cmd" %*
exit /b %errorlevel%
