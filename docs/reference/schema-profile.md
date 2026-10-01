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

# Weave v1alpha1 schema profile

A schema is the contract for a JSON value: which properties it has, which are
required, and which types or bounds they must satisfy. Workflow `inputSchema`
checks values supplied by callers; `outputSchema` checks the result. Action and
signal schemas apply at their corresponding boundaries. This page lists the
supported rules and limits. Start with [workflow authoring](../guides/workflow-authoring.md)
for a complete YAML example, or the [standalone tutorial](../guides/standalone.md)
for a server run.

![Separate schema checks around echo input, transform, and output](../diagrams/authoring-schema-boundaries.svg)

Trace the value from left to right, then compare the two reference systems below. Selecting a string from a valid input object changes the result type; the output contract still expects an object. [Open the diagram at full size](../diagrams/authoring-schema-boundaries.svg).

## Check a small contract

The echo tutorial accepts exactly one string property. This independent Python
example checks the schema itself, then checks a good and bad input:

```python
from firefly_weave.compiler.schemas import validate_payload, validate_schema

schema = {
    "type": "object",
    "properties": {"message": {"type": "string"}},
    "required": ["message"],
    "additionalProperties": False,
}
assert validate_schema(schema, {}) == ()
assert validate_payload(schema, {"message": "Hello, Weave"}, {}) == ()
issues = validate_payload(schema, {"message": 42}, {})
print([(issue.code, issue.path) for issue in issues])
```

Expect `[("WV-SCHEMA-INVALID_INSTANCE", "/message")]` (Python may display single
quotes). The second argument to `validate_schema` and third to `validate_payload`
is a **bundle**, a mapping of local schema names to schema documents; `{}` means
this example has no referenced schemas.

`properties` defines allowed values when a property is present. `required` makes
it mandatory. `additionalProperties: false` rejects undeclared properties.
Omitting `required` does not make a property required; declaring `default` does
not insert a missing value. A workflow value expression uses `ref`; a schema uses
`$ref`. These are separate reference systems.

## Profile scope

User input/output, signal, connector configuration and authentication schemas use
a bounded subset of JSON Schema Draft 2020-12. This is not a claim of complete
Draft support. Validation imports no application, database, provider, or secret
implementation and never retrieves remote schemas. `jsonschema[format-nongpl]`
is a base dependency so supported format checkers are installed explicitly.

## Public interfaces

```text
export_schemas(*, extra_models=None) -> dict[str, JsonObject]
schema_snapshot(*, extra_models=None) -> bytes
validate_schema(schema, bundle, *, limits=SchemaLimits()) -> tuple[Diagnostic, ...]
validate_payload(schema, value, bundle, *, limits=SchemaLimits()) -> tuple[Diagnostic, ...]
validate_contract_payload(name, value, *, limits=DEFAULT_CONTRACT_LIMITS) -> tuple[Diagnostic, ...]
```

`export_schemas()` returns fresh schemas keyed by `definition`, `workflow`,
`action`, `connector`, `diagnostic`, `worker-implementation`, `worker-routing`,
`catalog-lock`, `executable`, and `compiled-artifact`, together with runtime,
worker, provider, and operational wire contracts.
Schemas derive from the strict Pydantic models, use camelCase aliases, declare the
Draft 2020-12 dialect, and have local IDs `<name>.schema.json`. Normalization
removes Pydantic's nonstandard discriminator annotation; union validation remains
represented by ordinary composition. A custom chain-schema generator preserves
constraints applied after value validators. Internal string-constraint definition
names are normalized by content to remove process-specific addresses. Literal
`const`, `default`, and `examples` objects are never interpreted or rewritten as
schemas. `schema_snapshot()` returns RFC 8785 canonical bytes for release
artifacts; it does not write generated files into the repository.

`extra_models` is a trusted mapping of unique lowercase names to Pydantic model
classes or TypeAdapters. Built-in exports already include worker release, lease,
claim, completion, failure, and credential contracts. Additional models cannot
overwrite core names. Exporting a trusted model does not make an author-supplied schema trusted.

The recursive Step, Expression, and JSON-value definitions in generated outer
schemas are allowed only in `validate_contract_payload`, which selects a built-in
schema by name and applies bounded validation followed by its strict model.
Cross-field retry ordering, source-range ordering, omission/null rules, strict
Python integer typing, safe JSON numbers, and Unicode scalar checks remain part
of the full contract boundary. Plain Draft validators cannot represent every
model invariant. User schemas retain Draft integer semantics (`1.0` satisfies
`type: integer`), whereas typed definition fields require actual integers.

Outer shape validation does not validate schemas carried *as data* inside fields
such as `inputSchema`. The compiler must separately pass those field values to
`validate_schema`. Successful shape/schema checks do not authorize deployment or
replace dependency and semantic analysis. Generated schemas passed to the user
profile are rejected if recursive.

## Supported vocabulary

| Category | Keywords |
| --- | --- |
| Local resources | `$schema`, `$id`, `$ref`, `$defs`, `$comment` |
| Types and equality | `type`, `enum`, `const` |
| Numbers | `minimum`, `maximum`, `exclusiveMinimum`, `exclusiveMaximum`, `multipleOf` |
| Strings | `minLength`, `maxLength`, `pattern`, `format` |
| Objects | `properties`, `patternProperties`, `additionalProperties`, `required`, `minProperties`, `maxProperties`, `propertyNames`, `dependentRequired`, `dependentSchemas` |
| Arrays | `items`, `prefixItems`, `minItems`, `maxItems`, `uniqueItems`, `contains`, `minContains`, `maxContains` |
| Composition | `allOf`, `anyOf`, `oneOf`, `not`, `if`, `then`, `else` |
| Annotations | `title`, `description`, `default`, `examples`, `deprecated`, `readOnly`, `writeOnly`, `x-secret` |

Boolean subschemas are supported; public root schemas are JSON objects. The
only accepted explicit `$schema` is
`https://json-schema.org/draft/2020-12/schema`. Formats `date-time`, `uuid`, `uri`,
and `email` are checked explicitly. As in Draft, format applies only to strings.
Unknown formats are errors even when the current instance is not a string.
Metadata and annotation contents remain JSON data. `x-secret` must be boolean;
all diagnostics redact values regardless of that marker.

The accepted dialect annotation is checked at every schema node, including
bundled resources. Validation then removes those annotations only from an owned
evaluation copy, so jsonschema descent/reference/conditional evaluation cannot
switch to a stock validator and escape the custom work and regex bounds.
Schema-shaped literal values remain data and are not stripped. Caller schemas,
bundles, and payloads are not mutated.

All other schema keywords are rejected, including `$dynamicRef`,
`$dynamicAnchor`, `$recursiveRef`, `$recursiveAnchor`, `$anchor`, `$vocabulary`,
`unevaluatedItems`, `unevaluatedProperties`, content-encoding keywords, and legacy
`definitions`. Unsupported schema keywords are inspected only at schema-bearing
locations, not inside literal values.

## Local references

A reference is `#`, `#/json/pointer`, a bundle key, or a bundle key followed by
`#/json/pointer`. Bundle keys are simple filenames/identifiers composed of ASCII
letters, digits, underscores, hyphens, and nonempty dot-separated components;
examples are `types.json` and `customer-v1.schema.json`. Slashes, backslashes,
schemes, parent traversal, percent encoding, and anchors are rejected. Pointer
segments support RFC 6901 `~0` and `~1` escaping. A pointer must target an actual
schema-bearing node, not an enum/const/default object that resembles a schema.

Root `$id` values use the same local-name rules. A bundled resource's supplied
`$id` must equal its bundle key. Nested IDs and root IDs that shadow bundle
resources are rejected. Unresolved resources are errors. The explicit
`referencing.Registry` contains only the supplied resources and uses a retriever
that always raises `NoSuchResource`; neither HTTP nor filesystem retrieval is
available. Every bundled resource is checked, including unused definitions.
Cycles are rejected in user schemas, including unused recursive `$defs`.

## Bounded regex subset

Patterns use search semantics; authors add `^`/`$` for whole-string checks.
Supported syntax includes literal Unicode characters, `.`, anchors, ordinary
character classes and ranges, alternation, capturing/noncapturing groups,
`?`, `*`, `+`, lazy quantifiers, and counted repetitions `{n}`, `{n,}`, `{n,m}`.
Escaped punctuation and `\d`, `\D`, `\s`, `\S`, `\w`, `\W`, `\b`, `\B`,
`\n`, `\r`, `\t`, `\f`, `\v` are accepted. Shorthand classes use ASCII semantics;
literal Unicode characters remain supported. Hex/Unicode escapes must be
expressed as literal characters instead.

Backreferences, lookaround, named groups, flags, atomic/possessive constructs,
recursion/subroutines, backtracking verbs, fuzzy matching, and engine-specific
escapes are rejected. Unsupported syntax is rejected before compilation.
Counted-repeat compilation cost is conservatively estimated as pattern length
multiplied by the maxima of all counted repetitions, including independent
repetitions. It must fit the work budget; this can reject some complex patterns
whose actual cost is lower. Costs are accumulated across schema patterns before
compilation and charged again when a runtime pattern is compiled.

Both `pattern` and `patternProperties` use the timeout-enabled `regex` adapter.
`additionalProperties` also uses that adapter to decide which names match
`patternProperties`; no fallback to Python's unbounded pattern matcher occurs.
Each match has a timeout and all matches share a cumulative time budget.

## Operator budgets

`SchemaLimits` is frozen configuration passed only as a keyword argument. It is
separate from definition manifests and from `Limits.max_expression_nodes`.
Definitions cannot raise it. Default values are:

| Field | Default | Measurement |
| --- | ---: | --- |
| `max_schema_bytes` | 1,048,576 | Compact UTF-8 JSON for the root plus the bundle wrapper/resources |
| `max_schema_nodes` | 10,000 | Root/bundle JSON containers and values, excluding mapping keys |
| `max_schema_depth` | 128 | Physical JSON container depth in the root/bundle wrapper |
| `max_expansion_nodes` | 100,000 | Sum of expanded schema-node visits across resource roots, including repeats through refs |
| `max_ref_depth` | 64 | Maximum expanded path length through schema children and references |
| `max_validation_work` | 100,000 | Runtime keyword calls, inspected collection entries, nested equality/uniqueness nodes, regex compilation costs, regex calls, and produced internal errors; separately caps aggregate preparation regex compilation cost |
| `max_payload_bytes` | 1,048,576 | Compact UTF-8 JSON bytes before serialization |
| `max_document_nodes` | 100,000 | Payload containers and scalars, excluding mapping keys |
| `max_depth` | 32 | Payload container nesting |
| `max_diagnostics` | 100 | Returned diagnostics including any overflow marker |
| `max_pattern_length` | 1,024 | Pattern Unicode code points |
| `regex_timeout_seconds` | 0.01 | Per match |
| `max_regex_seconds` | 0.1 | Aggregate matching elapsed time per payload validation |

The table describes author-schema defaults (`SchemaLimits()`). Trusted generated
outer contracts use the public `DEFAULT_CONTRACT_LIMITS`, an instance of
`SchemaLimits` with **`max_validation_work=5,000,000`**; every other table default
is unchanged. Generated recursive unions perform multiple alternative checks for
each step/expression, so they need a separate explicit work ceiling. This profile
accepts the established 1,000-step workflow and a shallow 9,991-expression workflow,
both well within source/depth ceilings, through `workflow` and `definition`.
Their measured work is approximately 439,000 and 901,000 units respectively.
Author-schema validation retains the 100,000-work ceiling.

Explicit `limits=` replaces the selected default exactly, including lower work
limits. Operators can use `dataclasses.replace(DEFAULT_CONTRACT_LIMITS, ...)`
to adjust other contract bounds while preserving its work default. Passing
`SchemaLimits(max_diagnostics=1)` explicitly also selects that object's author
work default; no implicit raising or merging of operator limits occurs.

These are independent bounds, not guarantees that every payload fitting its
byte limit fits its work budget. Expansion weights and path heights are memoized
without expanding a reference DAG into an allocated tree; cached subtrees cannot
bypass either bound. Work is charged before keyword evaluation. Unique items use
canonical JSON keys; decimal `multipleOf` uses exact integer ratios of the
published decimal values. Domain/size checks reject cycles, unsafe integers,
nonfinite floats, Python-only values, and lone Unicode surrogates before Draft
validation. Capacity failures return `WV-SCHEMA-RESOURCE_LIMIT`.

## Diagnostics

All issues have stage `schema`, severity `error`, and generic safe messages.
Schema errors use schema pointers; instance errors use payload pointers. Values,
constraint literals, format names, regexes, and underlying exception messages
are not emitted. Object path segments are retained only when the actual
validator schema-path provenance identifies the corresponding `properties`
declaration. Keys reached through `additionalProperties` or `patternProperties`
are represented as `*`, even if unrelated branches or unused bundle schemas
declare the same string. References, composition and arrays preserve this
location correspondence. Strict-model-only errors lack validator provenance,
so all their string location segments are conservatively masked; integer array
indices are retained. Source locations remain the compiler's parser/source-map
responsibility.

Stable codes are `WV-SCHEMA-INVALID_SCHEMA`, `INVALID_INSTANCE`, `INVALID_FORMAT`,
`UNSUPPORTED_FORMAT`, `UNSUPPORTED_KEYWORD`, `UNSUPPORTED_DIALECT`,
`UNSUPPORTED_PATTERN`, `INVALID_REF`, `RECURSIVE_REF`, `RESOURCE_LIMIT`, and
`DIAGNOSTICS_TRUNCATED`, each with the `WV-SCHEMA-` prefix. Schema preparation
returns its first error. Schema and payload mappings are copied in sorted key
order after domain/size bounds, before preparation/evaluation and truncation.
Arrays retain their original semantic order. Thus equivalent reordered mappings
produce the same first schema error and the same capped runtime subset. Retained
runtime issues are then sorted by pointer and code. If an additional issue
exceeds the cap, the last slot is replaced by
`WV-SCHEMA-DIAGNOSTICS_TRUNCATED`, including when the cap is one. An exact-cap
result has no overflow marker. The tuple interface indicates truncation through
that marker; it does not claim an exact omitted count after bounded iteration
stops. The compiler result envelope retains this distinction alongside parser
omission metadata.

## Durable secret classification

Present ordinary values governed by `x-secret: true` **or** `writeOnly: true`
are rejected, including null, false, empty strings/objects/arrays and strings
that resemble credential handles. This stronger interpretation of `writeOnly`
is a Weave execution restriction. An absent optional property is permitted;
a schema declaring it alone is valid. Defaults are never materialized.
Marked `default`, `examples`, `const` and `enum` literals are rejected before
catalog/source/draft persistence. Known expression bindings to marked targets
are rejected before storing source. Input/output checks include pinned action
and implementation contracts, local references, arrays and every possible
composition/conditional branch. Ambiguous or exhausted classification fails
closed within the existing schema/regex/value budgets.

`WV-SCHEMA-SECRET_VALUE` has a value-free message and root diagnostic path;
it never echoes submitted keys. Only the dedicated validated connection
`secretRef` field bypasses ordinary-value classification, while keeping its
schema/reference/grant checks. Existing explicitly authorized custom-worker
credential leasing remains available. This is schema classification, not an
arbitrary secret detector or a protected payload store. Authors must classify
sensitive data; comments, descriptions and unannotated text cannot be reliably
recognized. Confidential business data still needs operator-configured access,
encryption and retention controls; this release does not configure them.

The pure `operations.redaction.project` returns `available`, `value` and
explicit omission path/reason metadata. A classified/unclassifiable root is
withheld conservatively; ordinary null and the literal `[REDACTED]` remain
ordinary values. `project_operational_record` retains validated IDs, codes,
actor identity and timing metadata while withholding freeform reasons,
evidence/error text, payloads and payload fingerprints. These projections are
non-executable evidence and must never be passed to the kernel as intact data.
The [history/replay](history-and-replay.md) and [simulation](simulation.md)
references describe the current evidence and debug APIs.

Annotation literal admission retains every governing schema at the same instance
location through compositions and references. A default in one `allOf` branch
cannot bypass a secret marker in another; the same applies to nested properties
and array positions. Concrete property names retain their matching schemas;
unknown pattern-name overlap is conservatively unioned within the shared work
and regex budget.
