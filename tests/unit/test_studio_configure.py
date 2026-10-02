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

"""Profile creation binds Studio to the CLI login target without exposing credentials."""

import json

from click.testing import CliRunner

from firefly_weave.cli.studio import studio


def login_file(tmp_path, **overrides):
    path = tmp_path / "oauth.json"
    path.write_text(
        json.dumps(
            {
                "provider_id": "company",
                "issuer": "https://identity.example",
                "client_id": "studio",
                "target": "https://api.example",
                "account": "developer",
                **overrides,
            }
        )
    )
    return path


def test_configure_derives_target_and_does_not_overwrite(tmp_path):
    config = login_file(tmp_path)
    output = tmp_path / "studio-profile.json"
    args = ["configure", "--auth-config", str(config), "--output", str(output)]
    result = CliRunner().invoke(studio, args)
    assert result.exit_code == 0, result.output
    profile = json.loads(output.read_text())
    assert profile["base_url"] == "https://api.example"
    assert profile["auth_config"] == str(config.resolve())
    assert "issuer" not in profile and "account" not in profile
    original = output.read_bytes()
    result = CliRunner().invoke(studio, args)
    assert result.exit_code != 0
    assert output.read_bytes() == original


def test_configure_rejects_incomplete_scope_and_credential_fallback(tmp_path):
    config = login_file(tmp_path)
    output = tmp_path / "profile.json"
    base = ["configure", "--auth-config", str(config), "--output", str(output)]
    for options in (["--project", "00000000-0000-0000-0000-000000000001"], ["--credential-store", "file"]):
        result = CliRunner().invoke(studio, [*base, *options])
        assert result.exit_code != 0
        assert not output.exists()


def test_invalid_config_never_echoes_input(tmp_path):
    config = login_file(tmp_path, client_secret="sensitive-value")
    output = tmp_path / "profile.json"
    result = CliRunner().invoke(studio, ["configure", "--auth-config", str(config), "--output", str(output)])
    assert result.exit_code != 0
    assert "sensitive-value" not in result.output
    assert not output.exists()
