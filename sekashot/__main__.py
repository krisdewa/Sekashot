"""``python -m sekashot`` entry point.

``--capture`` and ``--full`` are the hot path: the compositor keybind spawns
this process on every Print Screen press, and its entire job is to hand seven
bytes to the instance that is already running.  Importing PyQt6 and building a
``QApplication`` costs ~250 ms, so those two arguments are answered with a plain
AF_UNIX socket *before* any Qt import happens.  Everything else (the tray
daemon, ``--settings``, ``--install-hotkey``) still goes through the real app.
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile

#: argument -> command understood by the running instance's IPC socket
_IPC_COMMANDS = {
    "-c": "capture",
    "--capture": "capture",
    "--full": "full",
}


def _socket_paths() -> list[str]:
    """Where ``QLocalServer('sekashot-<uid>')`` may live.

    Qt puts it in ``QDir::tempPath()``; the runtime dir is checked too in case a
    session uses a different temp directory for the two processes.
    """
    name = f"sekashot-{os.getuid()}"
    paths = [os.path.join(tempfile.gettempdir(), name)]
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        paths.append(os.path.join(runtime, name))
    return paths


def signal_running_instance(command: str) -> bool:
    """True when a live instance accepted *command* (uses no Qt at all)."""
    payload = command.encode() + b"\n"
    for path in _socket_paths():
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        try:
            sock.connect(path)
        except OSError:
            sock.close()
            continue
        try:
            sock.sendall(payload)
            sock.shutdown(socket.SHUT_WR)  # let the server see the message end
        except OSError:
            pass
        finally:
            sock.close()
        return True
    return False


def main() -> int:
    argv = sys.argv[1:]
    if len(argv) == 1:
        command = _IPC_COMMANDS.get(argv[0])
        if command is not None and signal_running_instance(command):
            return 0

    from .app import main as run_app

    return run_app()


if __name__ == "__main__":
    sys.exit(main())
