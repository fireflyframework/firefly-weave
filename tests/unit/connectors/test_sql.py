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

"""Read-only SQL policy rejects unsafe statements and preserves parameter bounds."""

import importlib.util

import pytest


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT customer_id FROM business.customers; DELETE FROM business.customers",
        "SELECT customer_id FROM business.customers -- tail",
        "SELECT pg_sleep(:seconds)",
        "WITH x AS (DELETE FROM business.customers RETURNING customer_id) SELECT customer_id FROM x",
        "SELECT customer_id INTO another FROM business.customers",
        "SELECT :column FROM business.customers",
        "SELECT customer_id FROM :table",
        "SELECT * FROM business.customers",
        "DELETE FROM business.customers",
        "SELECT customer_id FROM business.customers WHERE customer_id = ':value'",
        "SELECT customer_id FROM business.customers WHERE customer_id = :value::text",
        "SELECT customer_id FROM pg_catalog.pg_roles",
        'SELECT "customer_id" FROM business.customers',
    ],
)
def test_restricted_grammar_rejects_unsupported_syntax(statement):
    assert importlib.util.find_spec("firefly_weave.connectors.sql") is not None, "SQL grammar absent"
    from firefly_weave.connectors.sql import parse_statement

    with pytest.raises(ValueError):
        parse_statement(statement)


def test_ast_binds_repeated_names_as_values():
    assert importlib.util.find_spec("firefly_weave.connectors.sql") is not None, "SQL grammar absent"
    from firefly_weave.connectors.sql import parse_statement

    plan = parse_statement(
        "SELECT customer_id FROM business.customers WHERE customer_id = :value AND status = :value LIMIT :limit"
    )
    assert plan.parameters == ("value", "limit")
    assert (
        plan.sql
        == 'SELECT "customer_id" FROM "business"."customers" WHERE "customer_id" = $1 AND "status" = $1 LIMIT $2'
    )


@pytest.mark.parametrize(
    "value",
    [
        "postgresql://localhost:5432",
        "postgresql://user:secret@127.0.0.1:5432",
        "postgresql://127.0.0.1:5432/db",
        "postgresql://127.0.0.1:5432?sslmode=disable",
        "postgresql://127.0.0.1:0",
        "postgresql:///tmp/socket",
    ],
)
def test_postgres_destination_rejects_dns_dsn_and_paths(value):
    from firefly_weave.connectors.postgresql import destination

    with pytest.raises(ValueError):
        destination(value)


def test_statement_source_and_token_bounds():
    from firefly_weave.connectors.sql import parse_statement

    for source in ["x" * 16385, "SELECT " + ", ".join(f"column_{i}" for i in range(65)) + " FROM business.table"]:
        with pytest.raises(ValueError):
            parse_statement(source)


def test_published_postgresql_example_compiles():
    from pathlib import Path

    from firefly_weave.compiler.api import compile_source
    from firefly_weave.compiler.catalog import CatalogSnapshot
    from firefly_weave.connectors.manifest import POSTGRES_DESCRIPTOR
    from firefly_weave.contracts.definitions import load_definition

    catalog = CatalogSnapshot.from_definitions(
        [load_definition(POSTGRES_DESCRIPTOR.manifest.value)],
        tasks=POSTGRES_DESCRIPTOR.capabilities,
        adapters=["weave-postgresql"],
    )
    result = compile_source(
        Path("examples/definitions/postgresql.action.yaml").read_text(), format="yaml", catalog=catalog
    )
    assert result.ok, result.to_bytes()
