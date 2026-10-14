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

"""The identity a worker reports when it registers.

It reads exactly five settings and never lists, logs or echoes the environment. Optional
values that are not valid (an unusual hostname, platform or Kubernetes name) are left out
instead of stopping the worker; labels the operator set explicitly fail clearly.
"""

from __future__ import annotations

import os
import platform
import socket
from collections.abc import Mapping
from importlib import metadata
from typing import Literal

from pydantic import TypeAdapter, ValidationError

from firefly_weave.contracts.workers import WorkerHost, WorkerIdentity, WorkerVersion

REPORT_HOST = "WEAVE_WORKER_REPORT_HOST"
LABELS = "WEAVE_WORKER_LABELS"
NAMESPACE = "WEAVE_POD_NAMESPACE"
NODE = "WEAVE_NODE_NAME"
KUBERNETES = "KUBERNETES_SERVICE_HOST"

_HOST: TypeAdapter[str] = TypeAdapter(WorkerHost)
_VERSION: TypeAdapter[str] = TypeAdapter(WorkerVersion)


def _bounded(adapter: TypeAdapter[str], value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return adapter.validate_python(value, strict=True)
    except ValidationError:
        return None


def _report_host(raw: str | None) -> bool:
    if raw is None:
        return True
    value = raw.strip().lower()
    if value in {"true", "1", "yes"}:
        return True
    if value in {"false", "0", "no"}:
        return False
    raise ValueError(f"{REPORT_HOST} must be true or false")


def _labels(raw: str | None) -> dict[str, str]:
    labels: dict[str, str] = {}
    for item in (raw or "").split(","):
        if not item.strip():
            continue
        key, separator, value = item.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not key or key in labels:
            raise ValueError(f"{LABELS} needs distinct key=value pairs separated by commas")
        labels[key] = value
    return labels


def _sdk_version() -> str | None:
    try:
        return metadata.version("firefly-weave")
    except metadata.PackageNotFoundError:
        return None


def worker_identity(
    *,
    runtime: Literal["embedded", "container", "process"] | None = None,
    environ: Mapping[str, str] | None = None,
) -> WorkerIdentity:
    """Describe this worker for its registration.

    `runtime` is what the caller knows about how it runs; without it a worker in a
    Kubernetes pod reports `container` and any other worker `process`. The host is the
    machine's hostname (the pod name in Kubernetes) unless `WEAVE_WORKER_REPORT_HOST=false`.
    """
    settings = os.environ if environ is None else environ
    kubernetes = bool(settings.get(KUBERNETES))
    chosen: Literal["embedded", "container", "process"] = runtime or ("container" if kubernetes else "process")
    hint: Literal["kubernetes", "docker", "none"] = (
        "kubernetes" if kubernetes else "docker" if chosen == "container" else "none"
    )
    host = _bounded(_HOST, socket.gethostname()) if _report_host(settings.get(REPORT_HOST)) else None
    labels = _labels(settings.get(LABELS))
    try:
        return WorkerIdentity(
            host=host,
            runtime=chosen,
            sdk_version=_bounded(_VERSION, _sdk_version()),
            language_version=_bounded(_VERSION, platform.python_version()),
            os=_bounded(_VERSION, platform.system()),
            arch=_bounded(_VERSION, platform.machine()),
            platform_hint=hint,
            namespace=_bounded(_HOST, settings.get(NAMESPACE)),
            node=_bounded(_HOST, settings.get(NODE)),
            labels=labels,
        )
    except ValidationError:
        pass
    # Raised outside the handler so no chained validation error repeats the rejected input,
    # which may be a value the operator meant to keep private.
    raise ValueError(
        f"{LABELS} accepts up to 8 labels. Keys have up to 64 letters, digits, '.', '_', '/' or '-' "
        "and do not name a token, password, secret or authorization. Values have up to 256 "
        "characters without control characters, and the whole identity stays within 4 KiB."
    )
