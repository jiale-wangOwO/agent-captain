@echo off
rem Legacy shortcut retained for existing installations; use run_agentcaptain.cmd.
call "%~dp0run_agentcaptain.cmd" %*
exit /b %errorlevel%
