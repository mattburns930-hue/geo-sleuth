"""HTTP helpers shared by service clients."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

PROXY_HELP = "proxy URL or 'direct'"

_IMAGE_MAGIC = ((b"\xff\xd8\xff", "jpeg"), (b"\x89PNG\r\n\x1a\n", "png"), (b"GIF87a", "gif"), (b"GIF89a", "gif"))


def image_kind(data: bytes) -> str | None:
    """Image type from magic bytes, or None (error pages, 'Host not in allowlist' text, empty bodies)."""
    for magic, kind in _IMAGE_MAGIC:
        if data.startswith(magic):
            return kind
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def is_image_file(path: Path) -> bool:
    """True when a cached file is a real image; error bodies saved under an image name return False."""
    try:
        with open(path, "rb") as f:
            return image_kind(f.read(16)) is not None
    except OSError:
        return False


def download_image(url: str, path: Path, proxy: str | None = None, timeout: int = 60,
                   headers: dict[str, str] | None = None) -> tuple[bool, str]:
    """Download an image into the cache only if the response is a real image.

    Returns (ok, reason). A valid cached file is reused; an invalid one (an earlier error body such as
    'Host not in allowlist', an HTML error page, an empty file) is deleted and fetched again. Failed
    responses never reach the cache, so a transient block can't poison later runs.
    """
    path = Path(path)
    if is_image_file(path):
        return True, "cached"
    path.unlink(missing_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".part{os.getpid()}")
    cmd = ["curl", "-q", "-sSL", "--max-time", str(timeout), *curl_args(proxy), "-o", str(tmp), "-w", "%{http_code}"]
    for name, value in (headers or {}).items():
        cmd += ["-H", f"{name}: {value}"]
    try:
        result = subprocess.run(cmd + [url], capture_output=True, text=True, timeout=timeout + 5)
    except (OSError, subprocess.TimeoutExpired):
        tmp.unlink(missing_ok=True)
        return False, "request timed out"
    code = result.stdout.strip()
    try:
        head = tmp.read_bytes()[:200] if tmp.exists() else b""
    except OSError:
        head = b""
    if result.returncode or not code.startswith("2") or image_kind(head) is None:
        tmp.unlink(missing_ok=True)
        if result.returncode:
            return False, f"connection failed (curl exit {result.returncode})"
        snippet = head.decode("utf-8", "replace").strip().splitlines()[0][:100] if head.strip() else "empty body"
        return False, f"HTTP {code}, not an image: {snippet}"
    os.replace(tmp, path)
    return True, "downloaded"


def resolve_proxy(proxy: str | None = None) -> str | None:
    value = (os.environ.get("GEO_PROXY", "") if proxy is None else proxy).strip()
    return None if value.lower() in ("", "direct") else value


def curl_args(proxy: str | None = None) -> list[str]:
    value = resolve_proxy(proxy)
    # Clear NO_PROXY for an explicit proxy; ignore ambient proxies for direct mode.
    return ["--proxy", value, "--noproxy", ""] if value else ["--noproxy", "*"]


def fetch_bytes(url: str, proxy: str | None = None, timeout: int = 20,
                headers: dict[str, str] | None = None) -> bytes:
    cmd = ["curl", "-q", "-fsSL", "--max-time", str(timeout), *curl_args(proxy)]
    for name, value in (headers or {}).items():
        cmd += ["-H", f"{name}: {value}"]
    result = subprocess.run(cmd + [url], capture_output=True, timeout=timeout + 5)
    if result.returncode:
        raise RuntimeError(f"Request failed (curl exit {result.returncode}); run doctor.py --network to check connectivity.")
    return result.stdout


def model_proxy_env(proxy: str | None = None) -> None:
    """Set the model downloader's process environment before importing its HTTP client."""
    value = resolve_proxy(proxy)
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        os.environ.pop(key, None)
    if value:
        os.environ.update(HTTP_PROXY=value, HTTPS_PROXY=value, ALL_PROXY=value)
    else:
        os.environ["NO_PROXY"] = "*"
