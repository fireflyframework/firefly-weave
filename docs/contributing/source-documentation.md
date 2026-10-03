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

# Document and attribute every source file

Use this guide when you add or change any file in the repository: code, tests,
documentation, diagrams, templates, or data. You learn which license header each
format needs, how to explain a Python module, how to declare a file that cannot
carry a comment, and how to read the coverage checker's report. It takes about
ten minutes to read. You need a source checkout and Python 3.12 or newer; the
checker uses only the standard library.

![Documentation and attribution validation workflow](../diagrams/documentation-contribution.svg)

**How to read this diagram:** Follow the four numbered steps from top to bottom:
establish what the code really does, explain it in order, run the checks, and read
the result as a reader would. The note at the bottom is why step 4 exists: a
passing check proves that the file is attributed and its links work, not that the
explanation is right.

[Open diagram at full size](../diagrams/documentation-contribution.svg)

First-party source is licensed under the Apache License 2.0 and names the Firefly
Software Foundation explicitly. Keep earlier copyright years that still apply, and
keep third-party ownership and notices exactly as they are: depending on a library
does not transfer its copyright to this project. The complete terms are in
[LICENSE](../../LICENSE) and [NOTICE](../../NOTICE).

## 1. Choose what each file needs

| File | What it needs |
| --- | --- |
| Python, including package `__init__.py` files, tests, migrations, and scripts | The `#` header, then a module docstring that explains the module's responsibility |
| Shell, TOML, YAML, INI, CFG, `Dockerfile`, `Makefile`, `.gitignore`, `.dockerignore` | The `#` header; a shebang line stays first |
| SQL | The header with `--` comments |
| Markdown, SVG, HTML, XML, and property lists | The header in one `<!-- … -->` comment at the top, after an XML declaration if there is one |
| TypeScript, JavaScript, CSS, SCSS, and Rust | The header in one `/* … */` comment at the top |
| Templates ending in `.tmpl` | The header for the format the template produces; for example, `adapter.py.tmpl` is checked as Python, including its docstring |
| Strict JSON, lockfiles, images, `py.typed`, and other formats without comments | An exact-path entry in the [source inventory](source-inventory.toml), as described in [step 4](#4-declare-files-that-cannot-carry-a-header) |
| A test fixture whose exact bytes or source positions a test asserts | An `immutable-fixture` inventory entry with its SHA-256 digest |

Tests, examples, migrations, scripts, and generator templates are source too.
When a template generates source, it should emit a header only if the generated
format allows comments.

## 2. Add the license header

Copy the header exactly; the checker compares every line. This is the Python form,
followed by the module docstring:

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

For other formats, keep the same lines and change only the comment syntax. Any
documentation page in `docs/` shows the `<!-- … -->` form, and any file under
`studio/src/app/` shows the `/* … */` form.

**Placement rules.** The header must come before any other content:

- A shebang such as `#!/bin/sh` stays on the first line, with the header right after it.
- A Python encoding declaration stays on the first or second line.
- The module docstring comes after the header and before the imports, and
  `from __future__` imports stay where Python requires them.
- An XML declaration stays first, with the header comment right after it.

Text inside a string, such as a header printed by a program, does not count.
Do not change a signed or hash-checked fixture just to add a header; declare it in
the inventory instead.

## 3. Explain each Python module

Every first-party Python module, including package initializers, needs a
docstring that explains its responsibility. Describe its public contracts and the
invariants a reader cannot see from one function, such as transaction ownership,
scope and row-level security, lease fencing, idempotency, concurrency, secret
redaction, bounded parsing, and side effects whose outcome can be unknown.

Do not narrate each line or add filler comments to satisfy a check. The checker
can only see that a docstring exists; a reviewer judges whether it helps.

## 4. Declare files that cannot carry a header

Strict JSON, `py.typed`, lockfiles, images, canonical payloads, and protocol
fixtures cannot hold a comment, and adding a comment field to a wire document
would change its contract. Record their ownership in the
[source inventory](source-inventory.toml) instead. The inventory keeps their bytes
unchanged.

Each file gets its own `[[exceptions]]` entry with its exact relative path.
Directory and wildcard entries are rejected. This is a real entry from the
inventory:

```toml
[[exceptions]]
path = "examples/worker/manifest.json"
kind = "commentless"
reason = "Strict JSON or canonical fixture; preserve parser contract and exact data bytes."
copyright = "Copyright 2026 Firefly Software Foundation."
author = "Firefly Software Foundation"
license = "Apache-2.0"
```

| Field | Required for | Value |
| --- | --- | --- |
| `path` | Every entry | The exact path from the repository root, with no wildcards and no `..` |
| `kind` | Every entry | One of the four kinds below |
| `reason` | Every entry | Why the file cannot carry a header |
| `copyright` | Every entry | For first-party files, `Copyright YEAR Firefly Software Foundation.` |
| `author` | Every kind except `third-party` | `Firefly Software Foundation` |
| `license` | Every entry | `Apache-2.0` for first-party files; the original license for third-party work |
| `source` | `generated` and `third-party` | The inventoried generator, or the third-party origin |
| `sha256` | `immutable-fixture` | The file's SHA-256 digest in lowercase hexadecimal |

Choose the kind that matches the file:

- **`commentless`**: a format that cannot safely carry a header, such as JSON, a
  lockfile, or an image. The checker rejects this kind for a format that can
  carry a comment, such as Python or Markdown.
- **`immutable-fixture`**: a test fixture whose bytes or source positions a test
  asserts. Only files under `tests/fixtures/` qualify. Any change to the bytes
  fails the strict check until someone reviews the fixture and updates its digest.
- **`generated`**: deterministic output. `source` must name the generator, which
  must itself be in the repository and covered.
- **`third-party`**: preserved external work with its original copyright,
  license, and `source`. Keep the upstream notices, and do not add Foundation
  authorship.

Most inventory entries are `commentless`: strict JSON, the Python, npm, and
Cargo lockfiles, the desktop icons and installer background, and `py.typed`. Two
YAML files are `immutable-fixture` entries because parser and CLI tests assert
their source positions. One entry is `generated`: Studio's schema test corpus, produced by
[`studio_schema_fixtures.py`](../../scripts/studio_schema_fixtures.py). No vendored
third-party source is declared. Third-party dependencies keep their own
distribution metadata and license terms; the inventory does not certify
redistribution compliance.

## 5. Run the coverage checker

From the repository root, run the strict check after every change that adds,
moves, or removes a file:

```sh
# Report every coverage issue; exit 1 if any remains.
python3 scripts/source_coverage.py --strict
```

Expected: one line per issue in the form `PATH: CODE: MESSAGE`, then a summary
such as `904 files; 69 excluded entries; 0 issues`. The counts grow with the
repository; the last number must be `0`.

The checker has three modes:

```sh
# Report without failing, for example while you work through a list of issues.
python3 scripts/source_coverage.py
# Write the full report as JSON outside the repository for review or tooling.
python3 scripts/source_coverage.py --format json > /tmp/weave-source-coverage.json
# Fail on any issue, as make check does.
python3 scripts/source_coverage.py --strict
```

Expected: the default mode always exits 0. Strict mode exits 1 when an issue
remains. Both exit 2 with `Invalid root/inventory configuration or unreadable
source directory.` when the root or the inventory cannot be read. A missing
default inventory is allowed for a new tree, but a missing file passed with
`--inventory` is an error.

**What the checker reads, and what it skips.** It never imports application code
or modifies a file. It skips version control, private working folders (including
`.local/` tutorial output), virtual environments, `node_modules`, build and
distribution output, browser-test reports, interpreter and tool caches, generated
desktop build output, `.env` and `.env.*` files, private `.pem` and `.key` files,
operating-system metadata, and the root `LICENSE` and `NOTICE` texts. The JSON
report lists each skipped path with its reason; the text report only counts them.
This protects local evidence and conventional secret paths,
but the checker is not a secret scanner or a publication allowlist. It reports
symbolic links instead of following them, and it checks the inventory file and its
parent folders for symbolic links before reading it.

## When the checker reports an issue

| Code | Why | What to do |
| --- | --- | --- |
| `missing-header` | The header is missing, incomplete, or not at the very top | Copy the header from step 2 into the file's comment syntax, before any other content |
| `missing-docstring` | A Python module has no docstring | Add a docstring after the header and before the imports |
| `python-syntax` | The Python file does not parse | Fix the syntax error; the docstring is checked afterward |
| `uncovered-format` | The checker does not know how to read a header in this format | Add an exact-path inventory entry, usually `commentless` |
| `invalid-exception` | An inventory entry lacks a required field, uses an unknown kind, names a comment-capable format as `commentless`, has the wrong first-party ownership, or declares an `immutable-fixture` outside `tests/fixtures/` or without a lowercase SHA-256 digest | Complete the entry using the field table in step 4 |
| `missing-generator` | A `generated` entry's `source` is not a scanned file in the repository, is a symbolic link, or names the entry itself | Point `source` at the covered generator |
| `fixture-drift` | An immutable fixture's bytes no longer match its digest | Confirm the change is intended and the tests still assert the right positions, then update `sha256` |
| `stale-exception` | An inventory entry names a file that no longer exists or is skipped | Remove the entry, or restore the file |
| `symlink` | The path is a symbolic link | Replace it with a real file, or review the link separately; links are never scanned |
| `unreadable-source` | The file is not valid text in its encoding | Fix the encoding, or declare the binary format in the inventory |

Do not hide an issue with an invented `third-party` or `generated` entry.

## Validate a contribution

The [Makefile](../../Makefile) is the authority for the project checks. `make
source` runs the strict coverage check on its own. `make check` runs it first,
then the documentation checks, the strict website build, Ruff, strict mypy, the
unit and contract tests, and the release package checks. Integration and
end-to-end suites need separately configured test services; see
[contributing](../../CONTRIBUTING.md#backend-and-end-to-end-checks).

If you change the coverage checker itself, run its own tests and lint:

```sh
# Run the checker's fixture tests.
uv run pytest tests/unit/test_source_coverage.py -q
# Lint and format-check the checker and its tests.
uv run ruff check scripts/source_coverage.py tests/unit/test_source_coverage.py
uv run ruff format --check scripts/source_coverage.py tests/unit/test_source_coverage.py
```

Expected: pytest reports that every test passed, Ruff prints
`All checks passed!`, and the format check reports the files as already formatted.

Never publish environment files, credentials, retained test evidence, dependency
folders, or temporary build and render output. Review the actual file lists of
the published site and the package; ignore rules alone do not prove that a
package is safe.

## Next steps

- [Build and maintain the documentation website](documentation-site.md): preview
  pages and follow the writing conventions.
- [Visual assets](../visual-assets.md): draw and check a diagram.
- [Contributing](../../CONTRIBUTING.md): the complete project checks.
