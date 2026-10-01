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

"""Shared offline transport policy; no application configuration is loaded."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

import click

from firefly_weave.compiler.api import CompileResult
from firefly_weave.compiler.catalog import FrozenDocument
from firefly_weave.contracts.diagnostics import Diagnostic

EXIT_SUCCESS = 0
EXIT_INVALID = 1
EXIT_USAGE = 2
EXIT_REMOTE = 3


class LocalError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def error_result(code: str, message: str) -> CompileResult:
    diagnostic = Diagnostic(code=code, message=message, stage="schema", severity="error", path="")
    return CompileResult((FrozenDocument.from_value(diagnostic.model_dump(by_alias=True)),), error_count=1)


def emit_result(result: CompileResult, output: str) -> None:
    if output == "json":
        click.echo(result.to_bytes().decode("utf-8"))
        return
    if result.partial:
        status = "passed" if result.validation_ok else "failed"
        click.echo(f"Partial validation {status}; catalog required for complete compilation; no executable produced.")
    elif result.ok:
        assert result.artifact is not None
        click.echo(f"Compilation passed: {result.artifact.digest}")
    else:
        click.echo("Validation or local operation failed; no executable produced.")
    for diagnostic in result.diagnostics:
        source = diagnostic.source
        location = f"{source.file or '<source>'}:{source.line}:{source.column}" if source else diagnostic.path or "/"
        click.echo(f"{location}: {diagnostic.severity} {diagnostic.code}: {diagnostic.message}")
    if result.truncated:
        click.echo(f"Diagnostics truncated; omitted={result.omitted_count}, schemaTruncated={result.schema_truncated}.")


def read_bounded(path: Path, limit: int) -> bytes:
    try:
        with path.open("rb") as stream:
            return stream.read(limit + 1)
    except OSError as error:
        raise LocalError("WV-CLI-READ", "Cannot read the requested local input file.") from error


def export_files(directory: Path, files: Mapping[str, bytes], *, force: bool) -> None:
    """Preflight the complete set, then publish files without following target symlinks."""
    try:
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise LocalError("WV-CLI-EXPORT", "Export destination must be a directory, not a file or symlink.")
        for name in files:
            target = directory / name
            if target.is_symlink() or (target.exists() and (not force or not target.is_file())):
                raise LocalError("WV-CLI-EXPORT", "Export targets already exist or are unsafe; use --force for files.")
        directory.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            temporary: str | None = None
            try:
                with tempfile.NamedTemporaryFile(dir=directory, delete=False) as stream:
                    temporary = stream.name
                    stream.write(data)
                if force:
                    os.replace(temporary, directory / name)
                else:
                    os.link(temporary, directory / name)
            finally:
                if temporary is not None and os.path.exists(temporary):
                    os.unlink(temporary)
    except OSError as error:
        raise LocalError("WV-CLI-EXPORT", "Cannot safely write the requested export files.") from error
