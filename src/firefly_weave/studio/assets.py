# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0

"""Version-pinned, checksum-verified optional Studio asset installation."""

from __future__ import annotations

import hashlib
import json
import re
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from uuid import uuid4

from firefly_weave import __version__

MAX_BUNDLE_BYTES = 64 * 1024 * 1024
MAX_FILE_BYTES = 16 * 1024 * 1024
MANIFEST = "studio-manifest.json"


def cache_root() -> Path:
    return Path.home() / ".cache" / "firefly-weave" / "studio"


def _safe(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(
        name
        and not path.is_absolute()
        and ".." not in path.parts
        and "\\" not in name
        and path.as_posix() == name
        and not any(p.startswith(".") for p in path.parts)
    )


def _manifest(raw: bytes) -> dict[str, str]:
    if len(raw) > 1024 * 1024:
        raise ValueError("Studio manifest exceeds size limit")
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("version") != __version__:
        raise ValueError("Studio bundle version must match the installed CLI")
    files = value.get("files")
    if not isinstance(files, dict) or not 1 <= len(files) <= 2048 or "index.html" not in files:
        raise ValueError("Studio manifest has no valid entry point")
    if any(
        not isinstance(n, str) or not _safe(n) or not isinstance(d, str) or not re.fullmatch(r"[a-f0-9]{64}", d)
        for n, d in files.items()
    ):
        raise ValueError("Invalid Studio asset manifest")
    return files


def verify_assets(directory: Path) -> Path:
    if directory.is_symlink():
        raise ValueError("Studio cache must not be a symlink")
    manifest = directory / MANIFEST
    if manifest.is_symlink() or not manifest.is_file() or manifest.stat().st_size > 1024 * 1024:
        raise ValueError("Studio bundle manifest is unavailable")
    files = _manifest(manifest.read_bytes())
    total = 0
    for name, digest in files.items():
        path = directory / name
        if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()) or not path.is_file():
            raise ValueError("Studio asset is missing or unsafe")
        size = path.stat().st_size
        total += size
        if size > MAX_FILE_BYTES or total > MAX_BUNDLE_BYTES or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("Studio asset integrity check failed; reinstall the matching bundle")
    return directory


def find_assets(*, cache: Path | None = None) -> Path:
    path = (cache or cache_root()) / __version__
    if not path.is_dir():
        raise ValueError("Studio is not installed. Run weave studio install --help, or use --assets with a local build")
    return verify_assets(path)


def install_bundle(archive: Path, sha256: str, *, cache: Path | None = None) -> Path:
    if not re.fullmatch(r"[a-f0-9]{64}", sha256) or archive.stat().st_size > MAX_BUNDLE_BYTES:
        raise ValueError("A bounded bundle and SHA-256 checksum are required")
    if hashlib.sha256(archive.read_bytes()).hexdigest() != sha256:
        raise ValueError("Studio bundle checksum does not match")
    base = cache or cache_root()
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        if len(members) > 2049 or len({m.filename for m in members}) != len(members):
            raise ValueError("Studio bundle has duplicate or too many files")
        total = 0
        for member in members:
            total += member.file_size
            mode = member.external_attr >> 16
            if (
                not _safe(member.filename)
                or member.is_dir()
                or stat.S_ISLNK(mode)
                or member.file_size > MAX_FILE_BYTES
                or total > MAX_BUNDLE_BYTES
            ):
                raise ValueError("Unsafe or oversized Studio bundle member")
        files = _manifest(bundle.read(MANIFEST))
        if {m.filename for m in members} != {MANIFEST, *files}:
            raise ValueError("Studio bundle content differs from manifest")
        base.mkdir(parents=True, exist_ok=True)
        target = base / __version__
        if target.is_symlink():
            raise ValueError("Studio cache must not be a symlink")
        if target.exists():
            try:
                return verify_assets(target)
            except ValueError:
                pass
        with tempfile.TemporaryDirectory(prefix=".install-", dir=base) as temporary:
            staging = Path(temporary) / "assets"
            staging.mkdir()
            for member in members:
                data = bundle.read(member)
                if member.filename != MANIFEST and hashlib.sha256(data).hexdigest() != files[member.filename]:
                    raise ValueError("Studio asset checksum mismatch")
                path = staging / member.filename
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            verify_assets(staging)
            backup = None
            if target.exists():
                backup = base / f"{__version__}.backup-{uuid4().hex}"
                target.rename(backup)
            try:
                staging.rename(target)
            except OSError:
                if backup is not None and not target.exists():
                    backup.rename(target)
                raise
    return target


def build_bundle(source: Path, output: Path) -> tuple[Path, str]:
    """Package a compiled application; the source tree and source maps stay private."""
    files: dict[str, bytes] = {}
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError("Studio build must not contain symlinks")
        if path.is_file() and path.suffix in {".html", ".js", ".css", ".svg", ".png", ".ico", ".woff2", ".txt"}:
            name = path.relative_to(source).as_posix()
            if not _safe(name) or path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError("Unsafe or oversized Studio build file")
            files[name] = path.read_bytes()
    if "index.html" not in files or len(files) > 2048 or sum(map(len, files.values())) > MAX_BUNDLE_BYTES:
        raise ValueError("A bounded Angular browser build with index.html is required")
    manifest = {"version": __version__, "files": {n: hashlib.sha256(d).hexdigest() for n, d in files.items()}}
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"firefly-weave-studio-{__version__}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name, data in files.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            bundle.writestr(info, data)
        bundle.writestr(
            zipfile.ZipInfo(MANIFEST, date_time=(2026, 1, 1, 0, 0, 0)), json.dumps(manifest, sort_keys=True)
        )
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".zip.sha256").write_text(f"{digest}  {archive.name}\n")
    return archive, digest
