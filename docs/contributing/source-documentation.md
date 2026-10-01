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

# Source documentation and attribution

## Choose the right documentation path

When adding or changing a file, work through this sequence before running the
checker:

1. Identify ownership and format. For first-party comment-capable source, use the
   native header below. Preserve third-party notices and exact protocol bytes.
2. For a Python module, explain its responsibility in a module docstring. For
   public behavior, update the owning guide/reference and its executable example
   where applicable. A license header does not explain how a feature works.
3. For strict JSON or an immutable fixture, add the exact path to the inventory
   using the appropriate kind below. Do not invent a comment field in a wire DTO.
4. Run `python scripts/source_coverage.py --strict` from the repository root.
   Use each reported path to fix a missing header, docstring, inventory entry, or
   unexpected file. For document edits, also run `python scripts/check_docs.py`
   to check local links and the documentation surface.
5. Read the rendered page/example as a user. Automated coverage establishes
   presence and structure; it cannot establish clear explanations, correct
   prerequisites, or a working end-to-end procedure.


First-party source uses Apache License 2.0 and identifies the Firefly Software
Foundation explicitly. Preserve earlier applicable copyright years, original
third-party ownership and notices. A dependency's presence does not transfer its
copyright to this project. See the complete [LICENSE](../../LICENSE) and
[NOTICE](../../NOTICE).

## Comment-capable files

Use the following header with the format's native comment syntax. Keep shebangs
first, Python encoding declarations on the first or second line, module docstrings
before imports, and `from __future__` imports in their required position.

```python
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
"""Describe this module's responsibility and its relevant boundaries."""
```

Use `#` for Python, shell, TOML, YAML, INI and build files, `--` for SQL, and an
HTML/XML comment for Markdown, SVG and templates. Preserve XML declarations.
Tests, examples, migrations, scripts and generator templates are source too.
When a template emits source, emit a suitable header only if the target format
allows comments. Do not alter signed or hash-sensitive fixtures merely to add
attribution.

Every first-party Python module, including package initializers, needs a meaningful
responsibility docstring. Explain public contracts and non-obvious invariants:
transaction ownership, scope/RLS, lease fencing, idempotency, concurrency,
secret redaction, bounded parsing and side-effect ambiguity. Do not narrate each
line or insert filler comments to satisfy a count. Automated checks can establish
presence and syntax; human review must judge whether documentation is useful.

## Commentless, immutable, generated and third-party files

Do not put comments or extra fields into strict JSON, `py.typed`, lockfiles,
canonical payloads, or protocol fixtures. The exact-path
[source inventory](source-inventory.toml) records their copyright, author, license
and reason while preserving their bytes. New files need new entries; directory
and glob exemptions are rejected.

Each `[[exceptions]]` entry has `path`, `kind`, `reason`, `copyright`, and `license`.
First-party entries also require `author = "Firefly Software Foundation"` and
`license = "Apache-2.0"`. Supported kinds:

- `commentless`: a format that cannot safely carry a source header.
- `immutable-fixture`: an exact test fixture whose source positions or bytes are
  asserted. Only files beneath `tests/fixtures/` qualify. Supply a SHA-256
  digest; any byte drift fails strict coverage until the fixture contract is
  reviewed and the inventory updated.
- `generated`: deterministic output, with `source` pointing to an inventoried local generator/source file. Review the generator's ownership and emitted format.
- `third-party`: preserved external work, with its original copyright, license and `source` provenance. Retain required upstream notices; do not add Foundation authorship.

A generated lockfile is covered as commentless to avoid changing its managed
bytes. Two YAML fixtures retain exact source positions asserted by parser/CLI
tests, so their bytes are hash checked rather than prefixed with comments. No
vendored third-party source is declared in the current inventory.
Third-party dependencies keep their own distribution metadata and license terms.
The inventory does not certify downstream redistribution compliance.

## Run the coverage checker

The checker uses only Python 3.12+ standard-library modules and never imports
application code or modifies inspected files:

```sh
python scripts/source_coverage.py
python scripts/source_coverage.py --format json > /tmp/weave-source-coverage.json
python scripts/source_coverage.py --strict
```

Default reporting exits 0 even when it finds gaps. Strict mode exits 1 for gaps;
invalid roots/inventory configuration or unreadable source directories exit 2
in either mode. A missing default inventory is allowed for a new tree; a missing
explicit `--inventory` path is a configuration error. Configuration files and
every existing ancestor are checked for symlinks before their contents are read.
Traversal failures are never treated as an empty, compliant tree. The sorted report
lists each inventoried file, every pruned entry, and actionable issue paths.
It checks the full Apache comment wording, Foundation copyright, explicit Author
and SPDX lines, Python syntax and nonempty module docstrings, including Python
generator templates. It reads each template using the intended output format.
Text in executable strings does not count as a header. Stale exceptions,
changed immutable fixtures and unknown formats fail strict mode. Symlinks are
reported and never followed, even for internal aliases; review aliases
explicitly at final publication.

The checker prunes version control, private working files (including `.local/`
tutorial output and credentials), virtual environments, dependency trees,
build/distribution outputs, interpreter/tool caches, `.env`/`.env.*`, private
`.pem`/`.key` files and operating-system metadata before reading contents. It
reports those paths and reasons. This protects local evidence and conventional
secret paths; it is not a secret scanner or a complete publication allowlist.
Unknown source formats remain visible instead of silently disappearing.

`make check` and `make check-release` run strict source coverage before the
normal validation targets. Every contribution must pass the same check. Do not hide gaps with invented third-party/generated declarations.

## Validate a contribution

Use the [Makefile](../../Makefile) as the authority for project checks. `make check`
runs strict source coverage, documentation navigation/SVG checks, unit/contract
tests, Ruff, strict mypy and a build. Integration and end-to-end
suites require explicitly configured, owned test services. To check only the
standalone inventory tool's fixtures:

```sh
uv run pytest tests/unit/test_source_coverage.py -q
uv run ruff check scripts/source_coverage.py tests/unit/test_source_coverage.py
uv run ruff format --check scripts/source_coverage.py tests/unit/test_source_coverage.py
```

Do not publish environment files, credentials, retained test evidence, dependency
directories or disposable build/render output. Review the exact publication and
package file lists; ignore rules alone do not establish safe packaging.
