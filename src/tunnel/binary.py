"""cloudflared 二进制探测 / 自动下载。

优先用系统已安装的 cloudflared；没有就下载到 DATA_DIR/bin 并验版本。
官方发布资产命名见 cloudflare/cloudflared releases。Windows ARM64 官方无
资产，明确报错而不是静默下载到不可执行的 x86 包。
"""

from __future__ import annotations

import contextlib
import os
import platform
import subprocess
import sys
import tarfile
import uuid
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import httpx

from .types import TUNNEL_RELEASE_BASE

MIN_BINARY_BYTES = 1024 * 1024
MAX_BINARY_BYTES = 200 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 180.0


class TunnelDownloadError(Exception):
    """下载/解压/校验失败：管理端点回 400 而不是 500（可换镜像重试）。"""


@dataclass(frozen=True)
class PlatformInfo:
    os_name: str          # linux / darwin / win32
    arch: str             # x64 / arm64 / arm / ia32


def platform_info() -> PlatformInfo:
    machine = platform.machine().lower()
    arch = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64",
            "armv7l": "arm", "i386": "ia32", "i686": "ia32"}.get(machine, machine)
    return PlatformInfo(os_name=sys.platform, arch=arch)


def asset_for(info: PlatformInfo) -> tuple[str, str] | None:
    """系统 → 官方发布资产名；None = 官方未提供该平台二进制。返回 (asset, kind)。"""
    if info.os_name == "linux":
        if info.arch == "x64":
            return "cloudflared-linux-amd64", "raw"
        if info.arch == "arm64":
            return "cloudflared-linux-arm64", "raw"
        if info.arch == "arm":
            return "cloudflared-linux-armhf", "raw"
        if info.arch == "ia32":
            return "cloudflared-linux-386", "raw"
    elif info.os_name == "darwin":
        if info.arch == "x64":
            return "cloudflared-darwin-amd64.tgz", "tgz"
        if info.arch == "arm64":
            return "cloudflared-darwin-arm64.tgz", "tgz"
    elif info.os_name == "win32":
        if info.arch == "x64":
            return "cloudflared-windows-amd64.exe", "raw"
        if info.arch == "ia32":
            return "cloudflared-windows-386.exe", "raw"
    return None


def download_url(asset: str, base: str | None = None) -> str:
    root = (base if base is not None
            else os.environ.get("CF_TUNNEL_MIRROR", "") or TUNNEL_RELEASE_BASE).strip()
    return f"{root.rstrip('/')}/{asset}"


def managed_path(data_dir: str, info: PlatformInfo) -> Path:
    binary = "cloudflared.exe" if info.os_name == "win32" else "cloudflared"
    return Path(data_dir) / "bin" / binary


def _executable(path: Path, info: PlatformInfo) -> bool:
    if not path.is_file():
        return False
    if info.os_name == "win32":
        return path.suffix.lower() == ".exe"
    return os.access(path, os.X_OK)


def system_candidates(info: PlatformInfo) -> list[Path]:
    """从 PATH 里找系统已安装的 cloudflared（Windows 下追加 .exe）。"""
    names = (["cloudflared.exe", "cloudflared"]
             if info.os_name == "win32" else ["cloudflared"])
    dirs = [item for item in os.environ.get("PATH", "").split(os.pathsep) if item]
    return [Path(dir_) / name for dir_ in dirs for name in names]


def probe_version(path: str | Path) -> str | None:
    """跑 `--version` 验证二进制可用；失败返回 None（不抛）。"""
    if not path:
        return None
    try:
        result = subprocess.run([str(path), "--version"], capture_output=True,
                                text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip().splitlines()[0].strip() if result.stdout.strip() else None


def resolve_binary(data_dir: str, info: PlatformInfo | None = None,
                   base: str | None = None) -> dict:
    """系统 PATH → DATA_DIR/bin → 自动下载；返回第一个可用二进制。"""
    info = info or platform_info()
    for candidate in system_candidates(info):
        if not _executable(candidate, info):
            continue
        version = probe_version(candidate)
        if version:
            return {"path": str(candidate), "source": "system", "version": version}
    managed = managed_path(data_dir, info)
    if _executable(managed, info):
        version = probe_version(managed)
        if version:
            return {"path": str(managed), "source": "managed", "version": version}
    return download_binary(data_dir, info, base)


def download_binary(data_dir: str, info: PlatformInfo | None = None,
                    base: str | None = None) -> dict:
    """下载官方资产到 DATA_DIR/bin；临时文件验版本通过后再原子改名。"""
    info = info or platform_info()
    asset = asset_for(info)
    if asset is None:
        raise TunnelDownloadError(
            f"官方未提供 {info.os_name}/{info.arch} 的 cloudflared 二进制，"
            "请手动安装后重试")
    asset_name, kind = asset
    url = download_url(asset_name, base)
    try:
        response = httpx.get(url, timeout=DOWNLOAD_TIMEOUT_SECONDS,
                             follow_redirects=True)
    except httpx.HTTPError as error:
        raise TunnelDownloadError(f"下载 cloudflared 失败: {error}") from None
    if not response.is_success:
        raise TunnelDownloadError(f"下载 cloudflared 失败: HTTP {response.status_code} {url}")
    raw = response.content
    if not MIN_BINARY_BYTES <= len(raw) <= MAX_BINARY_BYTES:
        raise TunnelDownloadError(f"cloudflared 下载文件异常（{len(raw)} 字节）")
    payload = extract_tgz(raw) if kind == "tgz" else raw
    final_path = managed_path(data_dir, info)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = final_path.with_name(f"{final_path.name}.{uuid.uuid4()}.tmp")
    try:
        tmp_path.write_bytes(payload)
        if info.os_name != "win32":
            os.chmod(tmp_path, 0o755)
        version = probe_version(tmp_path)
        if not version:
            raise TunnelDownloadError("下载的 cloudflared 无法通过版本校验")
        os.replace(tmp_path, final_path)
        return {"path": str(final_path), "source": "managed", "version": version}
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()


def extract_tgz(data: bytes) -> bytes:
    """最小 tar.gz 解包：取归档里名为 cloudflared 的成员，不引第三方依赖。"""
    try:
        with tarfile.open(fileobj=BytesIO(data), mode="r:gz") as archive:
            for member in archive.getmembers():
                if member.name.rsplit("/", 1)[-1] == "cloudflared":
                    extracted = archive.extractfile(member)
                    if extracted is not None:
                        return extracted.read()
    except (tarfile.TarError, OSError) as error:
        raise TunnelDownloadError(f"cloudflared 压缩包解压失败: {error}") from None
    raise TunnelDownloadError("cloudflared 压缩包中未找到 cloudflared 二进制")


def remove_managed(data_dir: str, info: PlatformInfo | None = None) -> None:
    """强制更新：删掉受管二进制，下次 resolve 会重新下载（系统安装的不动）。"""
    info = info or platform_info()
    with contextlib.suppress(FileNotFoundError):
        managed_path(data_dir, info).unlink()

