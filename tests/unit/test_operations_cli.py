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

"""Operations help and guided setup share the canonical API command families."""

from click.testing import CliRunner

from firefly_weave.cli.main import cli


def test_operations_help_explains_safe_order_and_exposes_plan_lifecycle():
    runner = CliRunner()
    result = runner.invoke(cli, ["operations", "--help"])
    assert result.exit_code == 0
    assert "Observe" in result.output and "runner" in result.output
    for name in ("targets", "deployments", "plans", "jobs", "runners"):
        result = runner.invoke(cli, ["operations", name, "--help"])
        assert result.exit_code == 0, result.output
    result = runner.invoke(cli, ["operations", "runner", "--help"])
    assert "setup" in result.output and "check" in result.output and "run" in result.output


def test_runner_check_invalid_file_emits_no_config_or_secret_contents(tmp_path):
    path = tmp_path / "invalid.json"
    path.write_text('{"secret":"do-not-echo"}')
    result = CliRunner().invoke(cli, ["operations", "runner", "check", "--config", str(path)])
    assert result.exit_code != 0
    assert "do-not-echo" not in result.output


def test_runner_setup_names_the_lumi_role_as_the_weave_ai_gateway(tmp_path):
    answers = [
        "https://weave.example",
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000002",
        "00000000-0000-0000-0000-000000000003",
        "00000000-0000-0000-0000-000000000004",
        "docker-compose",
        "/usr/local/bin/docker",
        "default",
        "weave",
        "daemon-id",
        "/srv/weave/compose.yaml",
        "/srv/weave/runner.lock",
        "weave-ai-gateway",
    ]
    result = CliRunner().invoke(
        cli,
        ["operations", "runner", "setup", "--output", str(tmp_path / "runner.json")],
        input="\n".join(answers) + "\n",
    )
    assert "Component role (lumi = Weave AI gateway) (api, worker, lumi):" in result.output
    assert "Lumi" not in result.output


def test_runner_check_and_run_load_the_private_origin_policy_before_the_configuration(tmp_path, monkeypatch):
    """An in-cluster runner's configuration names the API over plain HTTP, which only the private-origin file it
    is given approves, so both entry points install that file's policy before they read the configuration."""
    import json
    from uuid import uuid4

    from firefly_weave import private_origins as po
    from firefly_weave.contracts.deployments import ObservationReport
    from firefly_weave.deployment_runner import application

    origin = "http://weave-api.weave-system.svc:8000"
    entry = po.PrivateOrigin(origin=origin, purpose="runner", networks=("10.89.0.0/24",), credentials="bridge")
    origins = tmp_path / "private-origins.json"
    origins.write_bytes(po.render(po.PrivateOrigins(platform=po.PLATFORM).with_entries([entry])))
    config = tmp_path / "runner.json"
    config.write_text(
        json.dumps(
            {
                "base_url": origin,
                "scope": {"tenant_id": str(uuid4()), "project_id": str(uuid4()), "environment_id": str(uuid4())},
                "token_file": "/run/weave/secrets/token",
                "destination": {
                    "target_id": str(uuid4()),
                    "adapter": "kubernetes",
                    "external_identity": "namespace-uid",
                    "boundary": "weave-workers",
                    "executable": "/usr/local/bin/kubectl",
                    "context": "weave-runner",
                    "capabilities": ["observe"],
                    "components": [
                        {"name": "weave-worker", "container": "worker", "kind": "worker", "configuration": "local"}
                    ],
                    "image_repositories": ["127.0.0.1:5000/example-sleep"],
                },
            }
        )
    )

    observed, served = [], []

    class Adapter:
        async def observe(self):
            observed.append(True)
            return ObservationReport(resources=[], complete=True, settled=True)

    async def serve(settings, *, once):
        served.append((settings.base_url, once))

    monkeypatch.setattr(application, "adapter_for", lambda policy: Adapter())
    monkeypatch.setattr(application, "serve", serve)
    # The process already consulted an empty policy, so only an explicit install of the file's policy approves it.
    monkeypatch.setattr(po, "_installed", po.PrivateOrigins.empty())
    monkeypatch.setenv(po.ENV_FILE, str(origins))
    checked = CliRunner().invoke(cli, ["operations", "runner", "check", "--config", str(config)])
    assert checked.exit_code == 0, checked.output
    ran = CliRunner().invoke(cli, ["operations", "runner", "run", "--config", str(config), "--once"])
    assert ran.exit_code == 0, ran.output
    assert served == [(origin, True)] and observed == [True]
    # Without the file, the configuration itself is refused: the destination is never read.
    monkeypatch.delenv(po.ENV_FILE)
    refused = CliRunner().invoke(cli, ["operations", "runner", "check", "--config", str(config)])
    assert refused.exit_code != 0 and observed == [True]
