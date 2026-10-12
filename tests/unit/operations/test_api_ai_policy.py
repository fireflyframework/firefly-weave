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

"""API policy configuration, guarded reload and dependency ownership."""

import json

import pytest
from pydantic import ValidationError
from pyfly.container import Container

from firefly_weave import private_origins as po
from firefly_weave.app import make_app
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.ai_policy import APIAIPolicy
from firefly_weave.settings import Settings

DATABASE = "postgresql+asyncpg://unused:unused@127.0.0.1:1/unused"


def write_policy(path, endpoints=()):
    path.write_text(json.dumps({"version": 2, "endpoints": list(endpoints)}))
    path.chmod(0o600)


def test_absent_invalid_and_repaired_policies_are_distinct(tmp_path):
    assert APIAIPolicy().current() is None
    path = tmp_path / "ai.json"
    write_policy(path)
    owner = APIAIPolicy(path)
    assert owner.current().endpoints == ()
    path.write_text('{"version":2,"endpoints": ["secret-canary"')
    for _ in range(2):
        with pytest.raises(CatalogError, match="The AI policy is unavailable") as refused:
            owner.current()
        assert (refused.value.status, refused.value.code) == (503, "WV-AI-POLICY")
        assert "secret-canary" not in str(refused.value)
    write_policy(path)
    assert owner.current().endpoints == ()


def test_configured_missing_policy_is_unavailable_then_reloadable(tmp_path):
    path = tmp_path / "missing.json"
    owner = APIAIPolicy(path)
    with pytest.raises(CatalogError) as refused:
        owner.current()
    assert refused.value.code == "WV-AI-POLICY"
    write_policy(path)
    assert owner.current().endpoints == ()


def test_policy_replacement_updates_approval_and_never_uses_the_last_valid_policy(tmp_path):
    path = tmp_path / "ai.json"
    endpoint = {
        "id": "cloud",
        "label": "Cloud",
        "url": "https://models.example.com/v1",
        "providers": ["openai-chat"],
        "models": ["first"],
    }
    write_policy(path, [endpoint])
    owner = APIAIPolicy(path)
    old = owner.current()
    assert old.endpoints[0].approves("openai-chat", "first")
    replacement = tmp_path / "replacement.json"
    write_policy(replacement, [{**endpoint, "models": ["second"]}])
    replacement.replace(path)
    current = owner.current()
    assert current.sha256 != old.sha256
    assert current.endpoints[0].approves("openai-chat", "second")
    assert not current.endpoints[0].approves("openai-chat", "first")
    path.unlink()
    with pytest.raises(CatalogError):
        owner.current()


@pytest.mark.parametrize("value", [None, "", "ai-policy.json"])
def test_policy_setting_reads_the_environment(monkeypatch, tmp_path, value):
    if value:
        value = str(tmp_path / value)
    monkeypatch.setenv("WEAVE_DATABASE_URL", DATABASE)
    monkeypatch.delenv("WEAVE_AI_POLICY_FILE", raising=False)
    if value is not None:
        monkeypatch.setenv("WEAVE_AI_POLICY_FILE", value)
    assert Settings.from_env().ai_policy_file == (value or None)


def test_policy_setting_rejects_relative_paths():
    with pytest.raises(ValidationError, match="absolute"):
        Settings(database_url=DATABASE, ai_policy_file="relative/ai.json")


def test_isolated_service_graph_defaults_to_no_policy():
    container = Container()
    container.register(APIAIPolicy)
    assert container.resolve(APIAIPolicy).current() is None


async def test_application_registers_one_policy_owner_without_eager_file_validation(tmp_path):
    path = tmp_path / "ai.json"
    origins = po.PrivateOrigins(platform=po.PLATFORM).with_entries(
        [
            po.PrivateOrigin(
                origin="http://127.0.0.1:11434", purpose="model", networks=("127.0.0.1/32",), credentials="none"
            )
        ]
    )
    settings = Settings(database_url=DATABASE, ai_policy_file=str(path), private_origins=origins)
    with po.installed(po.PrivateOrigins.empty()):
        app = make_app(settings)
        try:
            context = app.state.pyfly.context
            owner = context.get_bean(APIAIPolicy)
            assert context.get_bean(APIAIPolicy) is owner
            with pytest.raises(CatalogError):
                owner.current()
            write_policy(
                path,
                [
                    {
                        "id": "local",
                        "label": "Local",
                        "url": "http://127.0.0.1:11434/v1",
                        "providers": ["openai-chat"],
                        "compat": "ollama",
                        "credential": "none",
                        "models": "served",
                    }
                ],
            )
            assert owner.current().endpoints[0].approves("openai-chat", "qwen3:4b")
        finally:
            await app.state.resources.close()
