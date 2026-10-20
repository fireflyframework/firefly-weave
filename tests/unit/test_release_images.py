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

"""Release image builds use one validated context and exact immutable output IDs."""

import importlib.util
from pathlib import Path

import pytest


def test_build_requires_owned_context_before_any_docker_call(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[2] / "scripts/build_release_images.py"
    spec = importlib.util.spec_from_file_location("build_release_images", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    monkeypatch.setattr(module, "run_command", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ValueError):
        module.build(tmp_path, tmp_path / "images", "default")
    assert calls == []


def stages(path):
    """FROM lines and the instructions of each stage of a Dockerfile."""
    result, current = {}, None
    for line in path.read_text().splitlines():
        if line.startswith("FROM "):
            parts = line.split()
            current = parts[-1] if "AS" in parts else parts[1]
            result[current] = [line]
        elif current is not None and line.strip() and not line.startswith("#"):
            result[current].append(line)
    return result


def test_runner_stage_pins_the_docker_cli_by_digest():
    root = Path(__file__).resolve().parents[2]
    runner = stages(root / "Dockerfile")["runner"]
    assert runner[0] == "FROM server AS runner"
    cli = "docker:27.5.1-cli@sha256:851f91d241214e7c6db86513b270d58776379aacc5eb9c4a87e5b47115e3065c"
    assert f"COPY --from={cli} /usr/local/bin/docker /usr/local/bin/docker" in runner
    assert (
        f"COPY --from={cli} /usr/local/libexec/docker/cli-plugins/docker-compose "
        "/usr/local/libexec/docker/cli-plugins/docker-compose"
    ) in runner
    assert runner[-2] == "USER 65532:65532"
    assert "RUN mkdir -p /run/weave-operations/state && chown 65532:65532 /run/weave-operations/state" in runner
    assert runner[-1] == (
        'ENTRYPOINT ["weave", "operations", "runner", "run", "--config", "/run/weave-operations/config/runner.json"]'
    )


def test_operations_worker_stage_runs_unprivileged():
    root = Path(__file__).resolve().parents[2]
    worker = stages(root / "docker/worker.Dockerfile")["operations-worker"]
    assert worker[0] == "FROM base AS operations-worker"
    assert "USER 65532:65532" in worker
    assert worker[-1] == 'CMD ["python", "-I", "/opt/weave/operations-worker/main.py"]'
    assert any("operations-worker.py" in line and "/opt/weave/operations-worker/main.py" in line for line in worker)
    # Both images verify the same release inputs, so both copy the fixture files.
    for path in (root / "Dockerfile", root / "docker/worker.Dockerfile"):
        copy = next(line for line in path.read_text().splitlines() if line.startswith("COPY --chmod=0644 *.whl"))
        assert "operations-worker.py" in copy and "operations-worker-manifest.json" in copy
