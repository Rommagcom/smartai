from __future__ import annotations

import json
import os
from typing import Any

import paramiko


def _resolve_connection_value(explicit: Any, env_name: str, fallback: str) -> str:
    value = str(explicit or "").strip()
    if value:
        return value
    return str(os.getenv(env_name) or fallback).strip()


def ubuntu_sandbox_ssh(
    command: str,
    get_pty: bool = False,
) -> str:
    command_text = str(command or "").strip()
    if not command_text:
        raise ValueError("command is required")

    resolved_host = "ubuntu-sandbox"
    resolved_port = 22
    resolved_user = "aismartsandbox"
    resolved_password = "sandbox_smartai"
    timeout = 60

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        client.connect(
            hostname=resolved_host,
            port=resolved_port,
            username=resolved_user,
            password=resolved_password,
            timeout=10,
            banner_timeout=10,
            auth_timeout=10,
            look_for_keys=False,
            allow_agent=False,
        )
        # nosec B601 - this skill is explicitly designed to run arbitrary commands in isolated sandbox.
        _, stdout, stderr = client.exec_command(command_text, timeout=timeout, get_pty=bool(get_pty))
        exit_status = int(stdout.channel.recv_exit_status())
        stdout_text = stdout.read().decode("utf-8", errors="replace")
        stderr_text = stderr.read().decode("utf-8", errors="replace")
    finally:
        client.close()

    payload = {
        "status": "ok" if exit_status == 0 else "error",
        "host": resolved_host,
        "port": resolved_port,
        "username": resolved_user,
        "command": command_text,
        "exit_code": exit_status,
        "stdout": stdout_text,
        "stderr": stderr_text,
    }
    return json.dumps(payload, ensure_ascii=True)
