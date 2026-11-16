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

"""The migrations form one line that ends at the schema version the server checks.

Two branches that each add a migration after the same head both pass on their own; merged, they leave two heads,
and ``alembic upgrade head`` refuses to choose. These tests catch that before any database runs.
"""

import re
from pathlib import Path

from alembic.script import ScriptDirectory

from firefly_weave.persistence.migrations import SCHEMA_VERSION

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


def claimed_revisions() -> list[str]:
    """The revision each migration file declares, in file order."""
    found = []
    for path in sorted((MIGRATIONS / "versions").glob("0*.py")):
        match = re.search(r'^revision = "([^"]+)"$', path.read_text(encoding="utf-8"), re.MULTILINE)
        assert match is not None, path.name
        found.append(match.group(1))
    return found


def test_the_migrations_have_one_head_and_it_is_the_schema_version():
    assert ScriptDirectory(str(MIGRATIONS)).get_heads() == [SCHEMA_VERSION]


def test_no_two_migrations_claim_the_same_revision():
    claimed = claimed_revisions()
    assert sorted(revision for revision in set(claimed) if claimed.count(revision) > 1) == []


def test_every_migration_is_on_the_line_from_the_schema_version_to_the_first():
    scripts = ScriptDirectory(str(MIGRATIONS))
    line = [script.revision for script in scripts.walk_revisions("base", SCHEMA_VERSION)]
    assert sorted(line) == sorted(claimed_revisions())
