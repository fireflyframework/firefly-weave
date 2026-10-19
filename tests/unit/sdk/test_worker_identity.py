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

"""A worker reports bounded identity from five explicit settings and nothing else."""

import logging
from collections.abc import Iterator, Mapping

import pytest

from firefly_weave.sdk import worker_identity as module
from firefly_weave.sdk.worker_identity import worker_identity

SETTINGS = {
    "WEAVE_WORKER_REPORT_HOST",
    "WEAVE_WORKER_LABELS",
    "WEAVE_POD_NAMESPACE",
    "WEAVE_NODE_NAME",
    "KUBERNETES_SERVICE_HOST",
}


class Recorder(Mapping[str, str]):
    """An environment that fails if it is listed and records each value read."""

    def __init__(self, values: dict[str, str]) -> None:
        self.values, self.read = values, []

    def __getitem__(self, key: str) -> str:
        self.read.append(key)
        return self.values[key]

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("worker identity must not list the environment")

    def __len__(self) -> int:
        raise AssertionError("worker identity must not measure the environment")


@pytest.fixture(autouse=True)
def fixed_host(monkeypatch):
    monkeypatch.setattr(module.socket, "gethostname", lambda: "worker-1")
    for name in SETTINGS:
        monkeypatch.delenv(name, raising=False)


def test_identity_can_omit_host(monkeypatch):
    monkeypatch.setenv("WEAVE_WORKER_REPORT_HOST", "false")
    value = worker_identity()
    assert value.host is None and "host" not in value.model_dump(mode="json", exclude_none=True)


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, "worker-1"),
        ("true", "worker-1"),
        ("TRUE", "worker-1"),
        ("1", "worker-1"),
        ("yes", "worker-1"),
        ("False", None),
        ("0", None),
        ("no", None),
        (" false ", None),
    ],
)
def test_report_host_setting(raw, expected):
    environ = {} if raw is None else {"WEAVE_WORKER_REPORT_HOST": raw}
    assert worker_identity(environ=environ).host == expected


def test_unclear_report_host_setting_is_refused_without_echoing_it():
    with pytest.raises(ValueError, match="WEAVE_WORKER_REPORT_HOST") as caught:
        worker_identity(environ={"WEAVE_WORKER_REPORT_HOST": "secret-ish"})
    assert "secret-ish" not in str(caught.value)


@pytest.mark.parametrize("host", ["-bad", "bad_host", "a" * 254, "", "host\nname"])
def test_invalid_hostname_is_omitted_instead_of_stopping_the_worker(monkeypatch, host):
    monkeypatch.setattr(module.socket, "gethostname", lambda: host)
    assert worker_identity(environ={}).host is None


def test_reads_only_its_five_settings():
    environ = Recorder(
        {
            "WEAVE_WORKER_LABELS": "team=payments",
            "KUBERNETES_SERVICE_HOST": "10.0.0.1",
            "WEAVE_POD_NAMESPACE": "weave-workers",
            "WEAVE_NODE_NAME": "node-1",
            "DATABASE_URL": "postgresql://user:password@db/weave",
            "WEAVE_WORKER_TOKEN": "token-value",
        }
    )
    identity = worker_identity(environ=environ)
    assert set(environ.read) <= SETTINGS
    dumped = identity.model_dump_json()
    assert "password" not in dumped and "token-value" not in dumped
    assert identity.labels == {"team": "payments"}
    assert (identity.namespace, identity.node, identity.platform_hint) == ("weave-workers", "node-1", "kubernetes")


def test_process_defaults_and_reported_platform(monkeypatch):
    monkeypatch.setattr(module.platform, "python_version", lambda: "3.12.4")
    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(module.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(module.metadata, "version", lambda name: "0.1.0a15")
    identity = worker_identity(environ={})
    assert identity.model_dump(mode="json") == {
        "host": "worker-1",
        "runtime": "process",
        "sdk_version": "0.1.0a15",
        "language_version": "3.12.4",
        "os": "Linux",
        "arch": "x86_64",
        "platform_hint": "none",
    }


def test_unusual_platform_values_are_omitted(monkeypatch):
    monkeypatch.setattr(module.platform, "machine", lambda: "")
    monkeypatch.setattr(module.platform, "system", lambda: "x" * 65)
    monkeypatch.setattr(module.platform, "python_version", lambda: "3.12 (custom)")

    def missing(name):
        raise module.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(module.metadata, "version", missing)
    dumped = worker_identity(environ={}).model_dump(mode="json")
    assert not {"arch", "os", "language_version", "sdk_version"} & set(dumped)


@pytest.mark.parametrize(
    "runtime,environ,expected",
    [
        (None, {}, ("process", "none")),
        (None, {"KUBERNETES_SERVICE_HOST": "10.0.0.1"}, ("container", "kubernetes")),
        ("container", {}, ("container", "docker")),
        ("embedded", {}, ("embedded", "none")),
        ("embedded", {"KUBERNETES_SERVICE_HOST": "10.0.0.1"}, ("embedded", "kubernetes")),
        ("process", {"KUBERNETES_SERVICE_HOST": "10.0.0.1"}, ("process", "kubernetes")),
        (None, {"KUBERNETES_SERVICE_HOST": ""}, ("process", "none")),
    ],
)
def test_runtime_comes_from_the_caller_or_the_kubernetes_setting(runtime, environ, expected):
    identity = worker_identity(runtime=runtime, environ=environ)
    assert (identity.runtime, identity.platform_hint) == expected


def test_invalid_kubernetes_names_are_omitted():
    identity = worker_identity(environ={"WEAVE_POD_NAMESPACE": "ns/other", "WEAVE_NODE_NAME": "-node"})
    assert identity.namespace is None and identity.node is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", {}),
        ("  ", {}),
        ("team=payments", {"team": "payments"}),
        (" team = payments , tier=gold ,", {"team": "payments", "tier": "gold"}),
        ("app.example/name=weave,empty=", {"app.example/name": "weave", "empty": ""}),
        (",".join(f"k{index}=v" for index in range(8)), {f"k{index}": "v" for index in range(8)}),
    ],
)
def test_labels_are_parsed(raw, expected):
    assert worker_identity(environ={"WEAVE_WORKER_LABELS": raw}).labels == expected


@pytest.mark.parametrize(
    "raw",
    [
        ",".join(f"k{index}=v" for index in range(9)),
        "team=a,team=b",
        "=value",
        "team",
        "team payments=x",
        "api_token=hunter2-value",
        "db_password=hunter2-value",
        "secret=hunter2-value",
        "authorization=hunter2-value",
        "team=" + "x" * 257,
        "k" * 65 + "=hunter2-value",
        "team=line\x07hunter2-value",
    ],
)
def test_invalid_labels_fail_clearly_without_echoing_values(raw, caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ValueError, match="WEAVE_WORKER_LABELS") as caught:
        worker_identity(environ={"WEAVE_WORKER_LABELS": raw})
    message = str(caught.value) + repr(caught.value.__cause__) + repr(caught.value.__context__)
    assert "hunter2" not in message and "x" * 257 not in message
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert caplog.records == []


def test_identity_that_exceeds_storage_fails_without_echoing_values():
    raw = ",".join(f"key-{index}=" + "é" * 256 for index in range(8))
    with pytest.raises(ValueError, match="WEAVE_WORKER_LABELS") as caught:
        worker_identity(environ={"WEAVE_WORKER_LABELS": raw})
    assert "é" not in str(caught.value) and caught.value.__context__ is None


def test_identity_reads_the_process_environment_by_default(monkeypatch):
    monkeypatch.setenv("WEAVE_WORKER_LABELS", "team=payments")
    monkeypatch.setenv("WEAVE_POD_NAMESPACE", "weave-workers")
    identity = worker_identity()
    assert identity.labels == {"team": "payments"} and identity.namespace == "weave-workers"
