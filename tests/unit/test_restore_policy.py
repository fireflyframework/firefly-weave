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

"""Restore policy rejects unrelated databases and preserves database creation identity."""

import importlib.util
from pathlib import Path

import pytest


def restore_module():
    path = Path(__file__).resolve().parents[2] / "scripts/restore_database.py"
    spec = importlib.util.spec_from_file_location("restore_database", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://weave_b1_owner:secret@remote:55433/weave_b1_control",
        "postgresql+asyncpg://weave_b1_owner:secret@localhost:5432/weave_b1_control",
        "postgresql+asyncpg://someone:secret@localhost:55433/weave_b1_control",
        "postgresql+asyncpg://weave_b1_owner:secret@localhost:55433/production",
    ],
)
def test_restore_guard_rejects_unrelated_backends(url):
    with pytest.raises(ValueError):
        restore_module().guard_control(url)


def test_empty_target_preserves_owner_encoding_locale_and_never_migrates():
    module = restore_module()
    statement = module.create_database_sql(
        "weave_restore_" + "a" * 32,
        {
            "owner": "weave_b1_owner",
            "encoding": "UTF8",
            "collate": "C.UTF-8",
            "ctype": "C.UTF-8",
            "provider": "c",
        },
    )
    assert "TEMPLATE template0" in statement
    assert 'OWNER "weave_b1_owner"' in statement
    assert "ENCODING 'UTF8'" in statement and "LC_COLLATE 'C.UTF-8'" in statement
    assert "DROP" not in statement and "ALTER" not in statement
    with pytest.raises(ValueError):
        module.create_database_sql("existing_database", {})


def test_manifest_difference_identifies_changed_authority_or_data():
    module = restore_module()
    before = {"catalog": {"tables": [["run", "owner", ["app=r/owner"]]]}, "data": {"runs": {"rows": 1, "sha256": "a"}}}
    module.require_same_manifest(before, before)
    for changed in ({**before, "data": {}}, {**before, "catalog": {}}):
        with pytest.raises(ValueError, match="manifest"):
            module.require_same_manifest(before, changed)


# PostgreSQL 17 prints `CHECK(for_seconds BETWEEN 0 AND 3600 AND for_seconds%30=0)` with the BETWEEN as a nested
# AND; pg_restore parses that text again, which flattens the nesting. PRINTED is what PostgreSQL prints after it
# parses each definition once more.
NESTED = "CHECK ((((for_seconds >= 0) AND (for_seconds <= 3600)) AND ((for_seconds % 30) = 0)))"
FLAT = "CHECK (((for_seconds >= 0) AND (for_seconds <= 3600) AND ((for_seconds % 30) = 0)))"
WIDER = "CHECK ((((for_seconds >= 0) AND (for_seconds <= 7200)) AND ((for_seconds % 30) = 0)))"
OTHER_COLUMN = "CHECK ((((min_samples >= 0) AND (min_samples <= 3600)) AND ((min_samples % 30) = 0)))"
PRINTED = {
    NESTED: FLAT,
    FLAT: FLAT,
    WIDER: "CHECK (((for_seconds >= 0) AND (for_seconds <= 7200) AND ((for_seconds % 30) = 0)))",
    OTHER_COLUMN: "CHECK (((min_samples >= 0) AND (min_samples <= 3600) AND ((min_samples % 30) = 0)))",
}
# A CHECK the stand-in parser does not know, as if the target could not parse it: it must keep its own text.
UNPARSED = "CHECK ((preset_id ~ '^[a-z]+$'::text))"
FOREIGN_KEY = "FOREIGN KEY (rule_id) REFERENCES alert_rules(id)"


def alert_manifest(
    check=FLAT,
    *,
    table="alert_rules",
    name="alert_rules_for_seconds_check",
    unparsed=UNPARSED,
    foreign_key=FOREIGN_KEY,
    data="a",
):
    constraints = [[table, name, check]] if check else []
    constraints += [
        ["alert_rules", "alert_rules_preset_id_check", unparsed],
        ["alert_events", "alert_events_rule_fkey", foreign_key],
    ]
    return {
        "catalog": {"tables": [["public", "alert_rules", "r"]], "constraints": constraints},
        "data": {"alert_rules": {"rows": 1, "sha256": data}},
    }


SOURCE = alert_manifest(NESTED)


async def postgres_parser(definitions):
    """The text PostgreSQL prints for each CHECK definition it can parse on the target."""
    return {(table, text): PRINTED[text] for table, text in definitions if text in PRINTED}


def test_check_constraints_that_differ_only_in_and_nesting_compare_equal():
    import asyncio

    module = restore_module()
    restored = alert_manifest(FLAT)
    with pytest.raises(ValueError, match="manifest"):
        module.require_same_manifest(SOURCE, restored)
    asyncio.run(module.require_restored_manifest(SOURCE, restored, postgres_parser))


@pytest.mark.parametrize(
    "restored",
    [
        alert_manifest(WIDER),
        alert_manifest(OTHER_COLUMN),
        alert_manifest(None),
        alert_manifest(name="alert_rules_window_check"),
        alert_manifest(table="alert_rules_copy"),
        alert_manifest(unparsed="CHECK ((preset_id ~ '^[a-z0-9]+$'::text))"),
        alert_manifest(data="b"),
        alert_manifest(foreign_key=FOREIGN_KEY + " ON DELETE CASCADE"),
    ],
    ids=[
        "other-bounds",
        "other-column",
        "missing",
        "renamed",
        "other-table",
        "other-check-the-target-cannot-parse",
        "other-data",
        "other-foreign-key",
    ],
)
def test_restored_manifest_still_refuses_a_real_difference(restored):
    import asyncio

    module = restore_module()
    with pytest.raises(ValueError, match="manifest"):
        asyncio.run(module.require_restored_manifest(SOURCE, restored, postgres_parser))


def test_restore_cleanup_attempts_every_owned_resource():
    import asyncio
    import sys
    from types import SimpleNamespace

    path = Path(__file__).resolve().parents[1] / "e2e/support/restore_matrix.py"
    spec = importlib.util.spec_from_file_location("restore_cleanup_policy", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    calls = []

    async def close(name):
        calls.append(name)
        if name != "trial":
            raise RuntimeError(name)

    with pytest.raises(ExceptionGroup) as errors:
        asyncio.run(
            module.close_owned(*(SimpleNamespace(close=lambda n=n: close(n)) for n in ("receiver", "outbox", "trial")))
        )
    assert calls == ["receiver", "outbox", "trial"]
    assert [str(error) for error in errors.value.exceptions] == ["receiver", "outbox"]


@pytest.mark.parametrize("context,container", [("foreign", "a" * 64), ("colima-weave-tests", "b" * 64)])
def test_custom_restore_arguments_must_match_approved_receipt(monkeypatch, tmp_path, context, container):
    import asyncio

    from sqlalchemy import make_url

    module = restore_module()
    monkeypatch.setenv("WEAVE_RELEASE_BACKENDS", str(tmp_path / "owned.json"))
    monkeypatch.setenv("WEAVE_TEST_DOCKER_CONTEXT", "colima-weave-tests")
    monkeypatch.setenv("WEAVE_TEST_POSTGRES_CONTAINER", "a" * 64)
    monkeypatch.setattr(
        module,
        "guard_control",
        lambda _: make_url("postgresql+asyncpg://weave_b1_owner:x@127.0.0.1:55541/weave_b1_control"),
    )
    with pytest.raises(ValueError, match="receipt"):
        asyncio.run(module.restore(tmp_path / "absent.env", tmp_path / "backup", context, container))
    assert not (tmp_path / "backup").exists()
