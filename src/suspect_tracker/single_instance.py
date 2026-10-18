"""Single-instance guard for the suspect tracker console.

Changed 2026-10-14: Only one copy of the console may run at a time. There is exactly one
Titan Mini board on this machine, so a second window could only ever steal the COM port from
the first one and produce confusing "connection failed" errors.

Ownership is decided by a **Windows kernel mutex**. Unlike a lock file it needs no cleanup
path: the operating system releases it when the process ends, even after a crash or a
``taskkill``. The check uses only ``ctypes`` so it runs before PySide6 is imported -- the
second launch exits without paying the Qt startup cost or flashing an empty window.

The named pipe (:class:`QLocalServer`) has a single job: when the user launches the EXE again,
the already-running window is raised to the front instead of silently doing nothing.
"""

from __future__ import annotations

import ctypes
import sys

# Namespace for the kernel mutex. Both the source checkout and the packaged EXE use the same
# name on purpose, so a development run and a shipped build also exclude each other.
MUTEX_NAME = "TitanSuspectTracker.SingleInstance"
# Endpoint of the activation pipe (Windows: a named pipe, so nothing is left on disk).
SERVER_NAME = "TitanSuspectTracker-instance"
ACTIVATE_MESSAGE = b"activate"

_ERROR_ALREADY_EXISTS = 183

_kernel32 = None


def _kernel32_library():
    """Configured kernel32 (argtypes matter: a 64-bit HANDLE truncates without them)."""
    global _kernel32
    if _kernel32 is None:
        library = ctypes.WinDLL("kernel32", use_last_error=True)
        library.CreateMutexW.restype = ctypes.c_void_p
        library.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
        library.CloseHandle.restype = ctypes.c_bool
        library.CloseHandle.argtypes = (ctypes.c_void_p,)
        _kernel32 = library
    return _kernel32


class SingleInstance:
    """Owns the single-instance mutex and (in the winning process) the activation pipe."""

    def __init__(
        self,
        mutex_name: str = MUTEX_NAME,
        server_name: str = SERVER_NAME,
        log=None,
    ) -> None:
        self.mutex_name = mutex_name
        self.server_name = server_name
        # Optional sink (the startup log) so a failed guard leaves a trace instead of silently
        # letting a second window through.
        self._log = log if log is not None else (lambda _message: None)
        self._mutex_handle: int | None = None
        self._lock_file = None
        self._server = None

    # -- ownership ---------------------------------------------------------------------
    def acquire(self) -> bool:
        """Become the unique instance.

        Returns ``True`` for the first process and ``False`` for every later one. If the
        platform refuses to provide the primitive at all the app is allowed to start -- a
        broken guard must never lock the operator out of the console.
        """
        if sys.platform == "win32":
            return self._acquire_mutex()
        return self._acquire_lock_file()

    def _acquire_mutex(self) -> bool:
        kernel32 = _kernel32_library()
        # Win32 only sets the last error on FAILURE -- a successful CreateMutexW leaves whatever
        # value was there before, which could be a stale ERROR_ALREADY_EXISTS and make a fresh
        # launch refuse to start. Clearing it first is required, not cosmetic.
        ctypes.set_last_error(0)
        handle = kernel32.CreateMutexW(None, False, self.mutex_name)
        error = ctypes.get_last_error()
        self._log(
            f"Single-instance guard: CreateMutexW({self.mutex_name!r}) -> "
            f"handle={handle!r} last_error={error!r}"
        )
        if not handle:
            # The primitive is unavailable (packaging/security restrictions). Letting the app
            # start is better than locking the operator out, but the reason is now on record.
            self._log(f"Single-instance guard unavailable (error {error}); starting anyway")
            return True
        if error == _ERROR_ALREADY_EXISTS:
            # Somebody else already owns it. Closing our handle is enough; theirs is untouched.
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            self._log("Single-instance guard: another instance already owns the mutex")
            return False
        self._mutex_handle = handle
        return True

    def _acquire_lock_file(self) -> bool:
        # Fallback for non-Windows hosts (the project targets Windows; this keeps tests and
        # development on other platforms usable).
        try:
            from PySide6.QtCore import QDir, QLockFile
        except Exception:
            return True
        lock_path = QDir.temp().filePath(f"{self.mutex_name}.lock")
        lock = QLockFile(lock_path)
        lock.setStaleLockTime(30_000)
        if not lock.tryLock(100):
            return False
        self._lock_file = lock
        return True

    def release(self) -> None:
        """Drop the guard explicitly.

        Production code keeps the guard for the whole process lifetime and lets the OS reclaim
        it, but a real release is needed for tests and for any future restart-the-app path: the
        kernel mutex stays locked for as long as *any* handle to it is open, including ours.
        """
        if self._lock_file is not None:
            self._lock_file.unlock()
            self._lock_file = None
        if self._server is not None:
            self._server.close()
            self._server = None
        if self._mutex_handle is not None:
            _kernel32_library().CloseHandle(ctypes.c_void_p(self._mutex_handle))
            self._mutex_handle = None

    # -- activation pipe ---------------------------------------------------------------
    def start_server(self, on_activate) -> bool:
        """Listen for a second launch and call ``on_activate`` when one arrives."""
        try:
            from PySide6.QtNetwork import QLocalServer
        except Exception:
            return False

        server = QLocalServer()
        if not server.listen(self.server_name):
            # An endpoint left behind by an abnormal shutdown blocks reuse; clear it once.
            QLocalServer.removeServer(self.server_name)
            if not server.listen(self.server_name):
                return False
        server.newConnection.connect(lambda: self._drain(server, on_activate))
        self._server = server
        return True

    def _drain(self, server, on_activate) -> None:
        socket = server.nextPendingConnection()
        if socket is None:
            return

        def handle_request() -> None:
            payload = bytes(socket.readAll()).strip()
            if payload == ACTIVATE_MESSAGE:
                on_activate()

        socket.readyRead.connect(handle_request)
        socket.disconnected.connect(socket.deleteLater)


def notify_existing_instance(timeout_ms: int = 500, server_name: str = SERVER_NAME) -> bool:
    """Ask the running instance to show itself. ``False`` means it could not be reached."""
    try:
        from PySide6.QtNetwork import QLocalSocket
    except Exception:
        return False

    socket = QLocalSocket()
    socket.connectToServer(server_name)
    if not socket.waitForConnected(timeout_ms):
        return False
    socket.write(ACTIVATE_MESSAGE)
    socket.flush()
    socket.waitForBytesWritten(timeout_ms)
    socket.disconnectFromServer()
    return True
