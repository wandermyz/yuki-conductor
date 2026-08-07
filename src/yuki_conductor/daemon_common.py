"""Shared helpers for the platform-specific daemon adapters."""

import shutil

from yuki_conductor.config import ERR_LOG_FILE, LOG_FILE, build_web_frontend, project_dir

__all__ = ["find_uv", "build_web_frontend", "project_dir", "print_logs"]


def find_uv() -> str:
    uv_path = shutil.which("uv")
    if not uv_path:
        raise RuntimeError("uv not found on PATH")
    return uv_path


def print_logs() -> None:
    """Print log file paths and tail the stdout log — cross-platform."""
    print(f"Stdout: {LOG_FILE}")
    print(f"Stderr: {ERR_LOG_FILE}")
    if LOG_FILE.exists():
        print(f"\n--- Last 20 lines of {LOG_FILE.name} ---")
        lines = LOG_FILE.read_text().splitlines()
        for line in lines[-20:]:
            print(line)
