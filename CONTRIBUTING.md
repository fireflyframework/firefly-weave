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

# Contributing

Firefly Weave is an alpha Python project built on PyFly. Read the
[documentation index](docs/README.md), [architecture](docs/architecture.md), and
[source documentation and attribution policy](docs/contributing/source-documentation.md)
before changing public contracts or runtime behavior.

Use Python 3.12+ and the locked dependencies. The [Makefile](Makefile) is the
source of truth for current checks:

```sh
uv sync --locked --no-editable --all-extras --group dev
make check
```

This runs strict source inventory/header checks, unit/contracts, Ruff lint and
format, strict mypy and package build. For a focused change, first run its relevant
suite; the integrated delivery must still pass the full gate. Do not update lock
or upstream framework provenance casually to resolve an unrelated failure.

## Backend and end-to-end checks

`make check-integration` and `make check-e2e` require explicitly configured owned
PostgreSQL, identity, broker and/or container resources appropriate to their tests.
`make check-release` includes all gates and must fail when a required dependency
is absent. Inspect fixture prerequisites and use distinct resources/evidence paths;
do not run against production or reset retained services to make tests pass.

[Local runtime setup](docs/reference/local-runtime.md) explains provisioning.
End-to-end fixtures exercise installed wheels, admitted images, identities and
external-effect receivers; they are acceptance tooling rather than the introductory
compiler quickstart. Do not copy another checkout's private env files or immutable
image IDs. Keep failures and corrected reruns distinguishable.

## Review and hygiene

Preserve canonical wire names, diagnostic codes, revision/idempotency semantics,
scoped authority and pure-import boundaries. Add focused regression tests for
changed behavior and document public contracts and non-obvious invariants.
Every new first-party source file follows the existing Apache/Author/SPDX policy;
strict JSON and immutable fixtures use the exact licensing inventory mechanism.

Keep credentials, `.env` files, `.superpowers`, `.secrets`, `.local`, dependency
directories, local databases, logs and disposable builds/renders out of publication.
Review actual package and public-tree inventories rather than relying only on
ignore rules. Requested editable SVG diagrams are source deliverables. Follow the
[visual guide](docs/visual-assets.md); preserve the approved logo/banner identity.

Report security concerns through the [security policy](SECURITY.md), not an issue
containing credentials or exploit details. Contribution does not imply a response
SLA, stable API guarantee or production support commitment.
