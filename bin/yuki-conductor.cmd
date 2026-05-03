@echo off
rem Wrapper script to run yuki-conductor via uv on Windows.
uv run --project "%~dp0.." yuki-conductor %*
