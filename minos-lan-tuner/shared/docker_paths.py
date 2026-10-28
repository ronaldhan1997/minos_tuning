"""Host path to Docker volume source.

The official GATK template mounts ``pathlib.Path`` strings through the Docker
CLI argument list. Docker Desktop on Windows accepts a POSIX-style
``/c/Users/...`` source. This helper is what the worker uses when it needs to
show or prepare a mount. It does not add flags the official template does not
already pass.
"""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath


def to_docker_volume_arg(path: str, platform: str) -> str:
    """Return the volume source Docker should receive for ``path``.

    ``platform`` is ``win32`` or a POSIX name such as ``linux``. Tests pass it
    explicitly so the conversion does not depend on the machine running pytest.
    """
    if path is None or str(path).strip() == "":
        raise ValueError("path is empty")
    if platform != "win32":
        if "\x00" in path:
            raise ValueError("path contains a NUL byte")
        return str(path)

    raw = str(path)
    if raw.startswith("\\\\") or raw.startswith("//"):
        raise ValueError(
            "UNC paths are not converted. Map the share to a drive letter "
            "or run the worker inside WSL2, where the files are under /mnt."
        )
    windows = PureWindowsPath(raw)
    drive = windows.drive
    if len(drive) < 2 or not drive[0].isalpha() or drive[1] != ":":
        raise ValueError(f"Windows path needs a drive letter: {raw!r}")
    tail = PurePosixPath(windows.as_posix().split(":", 1)[1])
    return "/" + drive[0].lower() + tail.as_posix()
