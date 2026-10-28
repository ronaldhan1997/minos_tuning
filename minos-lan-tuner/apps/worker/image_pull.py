"""Download a study image and report how much of it has arrived.

``docker pull`` hides byte counts when it has no terminal. The engine API
sends each layer's current and total size, which the window can show.
"""

from __future__ import annotations

import json
import socket
import sys
import time
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import urlencode

_PHASES = ("Downloading", "Extracting")


def image_query(image: str) -> str:
    """Docker create-image query for a name:tag or name@digest reference."""
    if not image or any(char in image for char in "\r\n\x00 "):
        raise ValueError("image name is not valid")
    if "@" in image:
        name, digest = image.split("@", 1)
        fields = {"fromImage": name, "tag": digest}
    else:
        slash = image.rfind("/")
        colon = image.rfind(":")
        if colon > slash:
            fields = {"fromImage": image[:colon], "tag": image[colon + 1 :]}
        else:
            fields = {"fromImage": image, "tag": "latest"}
    return "/v1.41/images/create?" + urlencode(fields)


def progress_line(image: str, layers: dict[str, dict[str, int | str]]) -> str:
    """One status line for the phase that still has work left."""
    for phase in _PHASES:
        current = 0
        total = 0
        for layer in layers.values():
            if layer.get("phase") != phase or int(layer.get("total") or 0) <= 0:
                continue
            current += int(layer["current"])
            total += int(layer["total"])
        if total > 0:
            percent = min(100, int(100 * current / total))
            return f"{phase} {image} — {percent}% ({_size(current)} / {_size(total)})"
    return f"Downloading {image}"


def apply_event(image: str, layers: dict[str, dict[str, int | str]], event: dict[str, Any]) -> str | None:
    status = str(event.get("status") or "")
    layer_id = str(event.get("id") or "")
    detail = event.get("progressDetail") or {}
    if not isinstance(detail, dict):
        detail = {}
    if layer_id and status:
        layer = layers.setdefault(layer_id, {"current": 0, "total": 0, "phase": status})
        layer["phase"] = status
        if detail.get("current") is not None:
            layer["current"] = int(detail["current"])
        if detail.get("total") is not None:
            layer["total"] = int(detail["total"])
    if status not in _PHASES:
        return None
    return progress_line(image, layers)


def read_events(raw: bytes) -> list[dict[str, Any]]:
    """Decode one complete chunked Docker HTTP response into JSON events."""
    marker = b"\r\n\r\n"
    split = raw.find(marker)
    if split < 0:
        raise ValueError("Docker did not send a complete response")
    status_line = raw[:split].decode("iso-8859-1", "replace").split("\r\n", 1)[0]
    code = _status_code(status_line)
    if code >= 400:
        raise RuntimeError(status_line.strip() or "Docker rejected the download")
    events: list[dict[str, Any]] = []
    pending = b""
    for chunk in _chunks(raw[split + len(marker) :]):
        pending += chunk
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            item = _json_line(line)
            if item is not None:
                events.append(item)
    tail = _json_line(pending)
    if tail is not None:
        events.append(tail)
    return events


def pull_image(image: str, announce: Callable[[str], None]) -> tuple[bool, str]:
    """Pull one image. Announce again when the percent or phase changes."""
    try:
        path = image_query(image)
    except ValueError as exc:
        return False, " " + str(exc)
    try:
        handle = _open_docker()
    except OSError as exc:
        return False, " " + str(exc)
    layers: dict[str, dict[str, int | str]] = {}
    shown = ""
    try:
        handle.write(f"POST {path} HTTP/1.1\r\nHost: docker\r\nConnection: close\r\n\r\n".encode())
        handle.flush()
        reader = _Reader(handle, time.monotonic() + 3600)
        status = reader.readline()
        while True:
            line = reader.readline()
            if line == b"":
                break
        code = _status_code(status.decode("iso-8859-1", "replace"))
        if code >= 400:
            return False, " " + status.decode("iso-8859-1", "replace").strip()[:200]
        pending = b""
        while True:
            size_line = reader.readline()
            if not size_line:
                return False, " download stopped before Docker finished"
            size = int(size_line.split(b";", 1)[0].strip() or b"0", 16)
            if size == 0:
                return True, ""
            pending += reader.read(size)
            trailer = reader.read(2)
            if trailer not in {b"\r\n", b""}:
                pending += trailer
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                item = _json_line(line)
                if item is None:
                    continue
                error = item.get("error")
                if error:
                    return False, " " + str(error)[:200]
                text = apply_event(image, layers, item)
                if text and text != shown:
                    announce(text)
                    shown = text
    except (OSError, TimeoutError, json.JSONDecodeError, UnicodeError, ValueError) as exc:
        return False, " " + str(exc)[:200]
    finally:
        handle.close()


def _status_code(status_line: str) -> int:
    parts = status_line.split()
    if len(parts) < 2 or not parts[1].isdigit():
        raise RuntimeError(status_line.strip() or "Docker rejected the download")
    return int(parts[1])


def _json_line(line: bytes) -> dict[str, Any] | None:
    text = line.strip()
    if not text:
        return None
    item = json.loads(text)
    if not isinstance(item, dict):
        raise ValueError("Docker sent a progress event that is not an object")
    return item


def _chunks(body: bytes) -> Iterator[bytes]:
    remaining = body
    while remaining:
        line, sep, rest = remaining.partition(b"\r\n")
        if not sep:
            raise ValueError("incomplete chunk")
        size_text = line.split(b";", 1)[0].strip()
        if not size_text:
            remaining = rest
            continue
        size = int(size_text, 16)
        if size == 0:
            return
        if len(rest) < size:
            raise ValueError("short chunk")
        yield rest[:size]
        remaining = rest[size:]
        if remaining.startswith(b"\r\n"):
            remaining = remaining[2:]


class _Reader:
    def __init__(self, handle: Any, deadline: float) -> None:
        self.handle = handle
        self.deadline = deadline
        self.buf = b""

    def _fill(self) -> None:
        if time.monotonic() > self.deadline:
            raise TimeoutError("download took longer than an hour")
        block = self.handle.read(8192)
        if block:
            self.buf += block

    def readline(self) -> bytes:
        while b"\r\n" not in self.buf:
            before = len(self.buf)
            self._fill()
            if len(self.buf) == before:
                line = self.buf
                self.buf = b""
                return line
        line, self.buf = self.buf.split(b"\r\n", 1)
        return line

    def read(self, size: int) -> bytes:
        while len(self.buf) < size:
            before = len(self.buf)
            self._fill()
            if len(self.buf) == before:
                break
        out = self.buf[:size]
        self.buf = self.buf[size:]
        return out


def _open_docker() -> Any:
    if sys.platform == "win32":
        return open(r"\\.\pipe\docker_engine", "r+b", buffering=0)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(120)
    last: OSError | None = None
    for candidate in ("/var/run/docker.sock", "/run/docker.sock"):
        try:
            sock.connect(candidate)
            return sock.makefile("rwb", buffering=0)
        except OSError as exc:
            last = exc
    raise last if last is not None else OSError("Docker is not running")


def _size(amount: int) -> str:
    value = float(max(0, amount))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{amount} B"
