@echo off
REM Resonant Code Agent — run from any directory on Windows
REM Usage: resonant [options]
REM
REM Set RESONANT_API to your Mac Studio's IP:
REM   set RESONANT_API=http://10.0.0.133:8000
REM
REM Or pass it directly:
REM   resonant --api http://10.0.0.133:8000

if "%RESONANT_API%"=="" set RESONANT_API=http://10.0.0.133:8000

python -m resonant_client --api %RESONANT_API% %*
