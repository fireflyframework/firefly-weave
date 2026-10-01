<!--
Copyright 2026 Firefly Software Foundation.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
Author: Firefly Software Foundation
SPDX-License-Identifier: Apache-2.0
-->

# Definition contracts

Definitions use `apiVersion: weave/v1alpha1`, `kind: Workflow | Action | Connector`,
`metadata: {name, version}`, and `spec`. Names permit letters, digits, dots,
underscores, and hyphens. Versions follow SemVer 2.0, including prerelease and
build identifiers. Dependency references are exact `name@version` strings.
Unknown fields and unpublished snake_case wire aliases are rejected. Python
attributes are snake_case; input dictionaries and aliased constructors use the
published camelCase fields. Serialize with `model_dump(by_alias=True)` or
`model_dump_json(by_alias=True)`. Workflow `timeoutSeconds`, Action `connection`
and `routing`, and action-step `connection` may be omitted. When supplied they
must match their declared types; explicit null is rejected. Both serializers
preserve omission for these fields automatically, and their generated schemas
permit omission while excluding null. An omitted field is represented by None
on the Python model.

`load_definition` performs strict model/shape validation only. It does not parse
source, validate the JSON Schema dialect, resolve dependencies, compile, or
authorize deployment. These are subsequent compiler tasks. Contracts import no
PyFly application, provider, network, database, or secret implementation.

## Expressions and steps

Every expression has exactly one tag: `{literal: <JSON value>}`, `{ref:
<JSON Pointer>}`, `{object: {field: expression}}`, `{array: [expression]}`, or
`{op: {name, args: [expression]}}`. Operators are `eq`, `ne`, `lt`, `lte`, `gt`,
`gte`, `and`, `or`, `not`, `exists`, and `coalesce`. Arity/type/scope checks and
evaluation are checked by the compiler and expression evaluator. Pointers use RFC 6901 escaping; the empty root
pointer is accepted.

Every step has `id` and a discriminating `kind`:

| Kind | Other wire fields |
| --- | --- |
| `action` | Required `uses`, `with` expression; optional `connection` slot name |
| `transform` | Required `value` expression |
| `switch` | Nonempty `cases: [{when, steps, output}]`; required `default: {steps, output}` |
| `parallel` | Nonempty `branches: {name: {steps, output}}`; required positive integer `concurrency` |
| `wait` | Required positive integer `durationSeconds` |
| `signal` | Required `name`, positive integer `timeoutSeconds`, `payloadSchema` object |
| `fail` | Required business-error `code` and nonempty safe `message` |

Branches may contain zero steps but must declare output. The enclosing
workflow requires `inputSchema`, `outputSchema`, `steps`, and `output`;
`timeoutSeconds` is optional and positive when supplied. `connections` defaults
to `{}` and maps slot names to `{connector: <exact ref>, required: <bool>}`;
`required` defaults to `true`. Global ID uniqueness, signal-name uniqueness,
branch scope, output compatibility, and concurrency budgets are compiler checks.

## Actions and connectors

Action implementations are `{kind: worker, taskType, taskVersion}` or
`{kind: connector, uses: <exact connector ref>, action: <descriptor name>}`.
The Action spec requires `sideEffect`, positive `timeoutSeconds`,
`inputSchema`, and `outputSchema`. Optional `connection` uses the same
connection-requirement shape as workflow slots. Optional `routing: {queue}`
selects a named worker queue. `retry` defaults to `maxAttempts: 1`,
`initialDelaySeconds: 1`, `maxDelaySeconds: 30`; attempts include the initial
attempt, and the maximum delay cannot be smaller than the initial delay.
Side effects are `read_only`, `idempotent`, `idempotency_key`, or
`non_idempotent`. The declared side effect governs retry admission; it does not
prove that an external system implements idempotency.

A Connector spec requires an installed `adapter` identity, `configSchema`,
`authSchema`, `compatibility: {apiVersion: weave/v1alpha1}`, nonempty `actions`,
and adapter `limits`. Each named action descriptor requires `inputSchema`,
`outputSchema`, `sideEffect`, and `timeoutSeconds`, with the same retry defaults.
Adapter limits are positive integer `maxRequestBytes`, `maxResponseBytes`, and
`maxTimeoutSeconds`. These published I/O bounds are distinct from operator
compiler budgets. Manifests contain no Python source or executable import paths.

## Values, budgets, and diagnostics

The JSON domain preserves null, bool, int, float, string, list, and string-keyed
object values. Models reject nonfinite floats, integers outside
`[-9007199254740991, 9007199254740991]`, lone Unicode surrogates, bytes, and
Python-only objects. This applies to schema/literal JSON fields and numeric
definition fields. Models are frozen Pydantic models; nested lists/dictionaries
remain ordinary JSON containers, so immutability is shallow.

Operator `Limits()` defaults are source/payload 1,048,576 bytes, 1,000 steps,
depth 32, 100,000 document nodes, 10,000 expression nodes, and 100 diagnostics.
`max_document_nodes` bounds structural parsing of all value nodes (root,
containers, and scalars; mapping keys are excluded). `max_expression_nodes`
bounds expression semantics in the owning compiler stage and is not consumed by
parsing metadata, schemas, or steps. These operator budgets cannot be set through
workflow/action definitions. Model construction alone does not apply compilation
budgets.

Diagnostics carry `code`, `severity`, `stage`, `message`, JSON Pointer `path`,
optional `source`, `related` locations, optional `hint`, and optional
`suggestedEdit: {path, value}`. Severities are `error`, `warning`, `info`;
stages are `parse`, `schema`, `resolution`, `semantic`, `lowering`, `evaluation`.
Source ranges use optional `file`, required 1-based `line`/`column`, and optional
paired `endLine`/`endColumn`; the end cannot precede the start. Related locations
carry `path`, optional `source`, and optional `message`. The canonical models define the shape;
sorting, truncation, source mapping, and redaction are compiler responsibilities.
Parser failures expose `diagnostics` as a capped tuple, `omitted_count` as the
number of encountered issues excluded by that cap, and derived `truncated` as
`omitted_count > 0`. Compiler/CLI result envelopes preserve this
omission metadata. YAML source spans recognize CR/LF/CRLF, NEL, line separator,
and paragraph separator; JSON spans use CR/LF/CRLF only.

## Package and verification

`uv run` uses this project's `.venv`. Base dependencies support the offline
compiler. `server` adds PyFly web/security/relational/OIDC; `worker` adds HTTP and
telemetry; `integration` adds PostgreSQL, HTTP and Kafka clients. Server/worker
metadata pins the published **PyFly 26.9.15** GitHub wheel by verified direct URL
and SHA-256. `uv.lock` retains the artifact and transitive dependencies; no temporary
local source override or editable framework installation remains. See the
[release installation instructions](reference/local-runtime.md#runtime-version-and-lifecycle).
The optional `openapi` extra adds only PyFly base for offline native documentation.
`contracts.openapi.create_openapi_generator` lazily returns the public native
generator with the existing Weave constraint policy; base compiler and definition
schema exports remain PyFly-free. See [native OpenAPI](reference/native-openapi.md).
Provider specifics remain outside contracts.

`make check` runs strict source coverage, unit/contract tests, Ruff lint/format checks, strict mypy, and package
builds. Pytest has no implicit integration deselection: explicitly named real
backend tests must run and fail clearly if a required backend is absent. The
`integration` and `e2e` markers are registered. The current CLI and delivery
boundaries are described in the [documentation index](README.md).
