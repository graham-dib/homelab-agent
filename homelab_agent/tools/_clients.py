"""Shared client instances. One module so every tool reuses the same SSH connection."""
from __future__ import annotations

import os
from fabric import Connection
from invoke.exceptions import UnexpectedExit

from homelab_agent.config import settings

_dibo_conn: Connection | None = None


def dibo() -> Connection:
    """Return a singleton Fabric Connection to dibo.

    Lazily opens on first call. If the underlying socket has died, callers
    should catch the exception from .run() and call reset_dibo() to reconnect.
    """
    global _dibo_conn
    if _dibo_conn is None:
        _dibo_conn = Connection(
            host=settings.dibo_ssh_host,
            user=settings.dibo_ssh_user,
            port=settings.dibo_ssh_port,
            connect_kwargs={
                "key_filename": os.path.expanduser(settings.dibo_ssh_key_path),
            },
        )
    return _dibo_conn


def reset_dibo() -> None:
    """Force the next dibo() call to open a fresh connection.

    Use after a connection error to recover from a dropped socket.
    """
    global _dibo_conn
    if _dibo_conn is not None:
        try:
            _dibo_conn.close()
        except Exception:
            pass
        _dibo_conn = None


def run_on_dibo(cmd: str, warn: bool = False, timeout: int = 15) -> str:
    """Run a shell command on dibo and return stdout.

    Args:
        cmd: command string to execute via SSH
        warn: if True, non-zero exit codes don't raise (useful for grep, systemctl)
        timeout: seconds before the command is killed

    Returns:
        stdout as a string, stripped of trailing whitespace.

    Raises:
        UnexpectedExit on non-zero exit when warn=False
        Any paramiko/socket exception on connection failure (after one retry)
    """
    try:
        result = dibo().run(cmd, hide=True, warn=warn, timeout=timeout, in_stream=False)
    except Exception:
        reset_dibo()
        result = dibo().run(cmd, hide=True, warn=warn, timeout=timeout, in_stream=False)
    return result.stdout.strip()