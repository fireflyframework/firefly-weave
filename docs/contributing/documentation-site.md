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

# Build and maintain the documentation website

Use this guide when you add or change a documentation page. You preview the
website on your computer, run the same checks as the pull-request build, and
learn the writing conventions every page follows. The first run takes about ten
minutes, most of it installing dependencies.

**What you need:** a source checkout of this repository,
[uv](https://docs.astral.sh/uv/), and Python 3.12 or newer. You do not need
Node.js, Docker, or a running platform.

The website renders the same public Markdown that readers browse on GitHub.
[MkDocs](https://www.mkdocs.org/) and
[Material for MkDocs](https://squidfunk.github.io/mkdocs-material/) provide the
navigation, search, and code copying. One dark scheme, the Firefly Weave logo,
and Manrope served from the site's own files keep it aligned with Studio and the
brand. A small [build hook](../../scripts/docs_hook.py) adds the root project
pages, the approved artwork, and an API reference generated from the code.

## 1. Preview a change

Preview the site to see your page exactly as readers will, including navigation
and diagrams. From the repository root, run:

```sh
# Install the documentation tools on first use, then build and serve the site
# with live reload, reachable only from this computer.
uv run --locked --group docs --extra openapi mkdocs serve --dev-addr 127.0.0.1:8000
```

Expected: the first run installs the locked dependencies. Then MkDocs prints
`Documentation built in …` and `Serving on http://127.0.0.1:8000/firefly-weave/`.
Open that address; the `/firefly-weave/` path comes from the published site
address. Stop the server with Ctrl+C. `make docs-serve` runs the same
`mkdocs serve` command.

**A warning stops the preview build.** The configuration sets `strict: true`, so
a wrong link, anchor, or navigation entry fails `mkdocs serve` too. If the first
build has a warning, the command ends with
`Aborted with N warnings in strict mode!`; fix the warnings it lists (see
[When a check fails](#when-a-check-fails)) and start it again. If a later edit
causes a warning, the server keeps running, prints
`An error happened during the rebuild. The server will appear stuck until build errors are resolved.`,
and keeps showing the last good build until you fix the warning.

**Live reload watches only `docs/` and `mkdocs.yml`.** Restart the preview after
you change a root page (`README.md`, `CONTRIBUTING.md`, `SECURITY.md`,
`CHANGELOG.md`), the build hook, or Python code that the API reference is
generated from.

The build installs the Weave package with its OpenAPI extra to generate the full
API reference. It does not start the API, a database, an identity provider, or
workers. `uv run` adds the packages it needs without removing others. If another
process uses the checkout's Python environment, set `UV_PROJECT_ENVIRONMENT` to a
separate directory before running these commands, so the documentation tools do
not change that environment at all.

## 2. Check your change before you submit it

Run the same checks as the pull-request build, from the repository root:

```sh
# Build every page; any warning fails the build.
uv run --locked --group docs --extra openapi mkdocs build --strict
# Check links, heading anchors, reachability from README.md, and diagram structure.
python3 scripts/check_docs.py
# Check license headers and the source inventory for every file you added.
python3 scripts/source_coverage.py --strict
# Run the tests for link translation, private-file boundaries, and site navigation.
uv run --locked --group docs --extra openapi pytest tests/unit/test_docs_site.py tests/unit/test_docs_inventory.py -q
```

Expected:

- MkDocs ends with `Documentation built in …` and writes the site to
  `build/docs-site/`. That directory is generated and ignored by Git; do not
  commit it. `make docs` runs the same build.
- `check_docs.py` prints a JSON report whose `errors` list is empty, `[]`.
- `source_coverage.py` ends with `… files; … excluded entries; 0 issues`.
- pytest reports that every test passed.

`make check` runs all of these and the code checks; see
[contributing](../../CONTRIBUTING.md) for the complete gate. Passing checks prove
that the page renders and its links resolve. They do not prove that a documented
command works, so run every new example yourself.

## 3. Add or update a page

1. **Edit or create the Markdown under `docs/`.** Start every new page with the
   Apache-2.0 header comment that the other pages use; the source check rejects a
   page without it.
2. **Make the page reachable.** Add it to the explicit navigation in
   [mkdocs.yml](../../mkdocs.yml), and link to it from a related guide.
   `check_docs.py` requires every page to be reachable by links from the root
   `README.md`.
3. **Keep links relative to the source file**, including links to code and project
   files outside `docs/`. Use ordinary Markdown headings, links, images, and
   tables.
4. **Keep inbound links working.** Renaming a heading changes its anchor. Search
   the repository for `PAGE.md#old-anchor` and update every link you find.
5. **Add new diagrams to the catalog.** Put the SVG in `docs/diagrams/`, follow
   [visual assets](../visual-assets.md#draw-or-change-a-technical-diagram), and add a
   row to the [visual guide](../visual-guide.md).
6. **Look at the rendered page** at desktop and phone widths; diagrams sit on
   paper panels on the dark site. Try search, keyboard navigation, code copying,
   and the page's diagram links.

Do not wrap Markdown captions or links in raw HTML. Markdown link processing can
stop inside raw HTML, so a page may look correct in one renderer and break in
another.

## 4. Write in the house style

Every page follows the same conventions, so readers learn them once:

| Convention | What to do |
| --- | --- |
| Audience and goal first | Open with one paragraph that says what the reader achieves, who the page is for, how long it takes, and what they need, with links to prerequisites |
| Title names a task | "Connect the CLI to a platform", not "Connection" |
| Numbered steps | For each step, say why in one sentence, show the command or the UI action, then give the expected result and what to do if it differs |
| Commands | Use `sh` code blocks. Put a `#` comment that says why above each command, and follow the block with an "Expected: …" sentence |
| Placeholders | Use capitals, such as `YOUR_TENANT_ID`, or read real values with `read -r` so readers never paste an invented ID |
| Emphasis | Use a bold lead-in sentence such as **Studio is a client.** instead of admonition boxes |
| Interface labels | Write Studio and desktop labels in **bold**, exactly as they appear in the code under `studio/src` or `desktop/src-tauri`; write menu paths as **Settings → Platforms** |
| Accuracy | Check every command, flag, field, label, and error code against the code or `weave … --help`. Never describe a behavior you have not verified |
| Honest status | Say what was verified and how. Do not claim that a provider, operating system, or flow works if nobody has run it |
| Release status | Describe the published release. Mark features that are newer than it, and link to [installing the current source](../installation.md#install-the-current-source); when a page explains how to work with an older client or server, name that release, such as "alpha6 or earlier" |
| Terms | Use the terms from [concepts](../concepts.md) consistently: workflow, run, step, action, connector, integration connection, activation, platform, workspace. Say "sign in", not "log in" |
| Diagrams | Embed the image, follow it with a reading guide of one or two sentences (such as one that starts with **How to read this diagram:**), then an "Open diagram at full size" link |
| Troubleshooting | End task pages with a table whose columns are "What you see", "Why", and "What to do" |
| Language | American English, second person, active voice, short sentences, no emojis |
| Private material | Never link to private plans, local settings, or agent instruction files, and never include real tokens or secrets in examples |

## How repository links become site links

The [site hook](../../scripts/docs_hook.py) rewrites links after Markdown is parsed
and before MkDocs validates them:

- Links between documentation pages and to approved artwork become site links.
- Links to source code and deployment templates open their location on GitHub.
- Code examples keep their literal contents.

The hook publishes a fixed list of root project documents (`README.md`,
`CONTRIBUTING.md`, `SECURITY.md`, `CHANGELOG.md`), the Kubernetes template notes,
and two fixture notes as virtual pages; it does not copy the repository into the
site. It publishes only the approved artwork from `assets/`. Private plans, local
settings, hidden files, and the internal implementation status are never
published, and a link to them fails the build. To publish another root document,
add it to the hook's list and to the navigation, then test its links.

Set `WEAVE_DOCS_REF` to a release tag or commit to point source and edit links at
that revision when you build a release snapshot. The normal site uses `main`.
This setting only changes links: it does not check out another revision or add a
version switcher, so check out the intended revision before you build.

## Publish through GitHub Pages

The [documentation workflow](../../.github/workflows/docs.yml) builds the locked
site on every pull request. On `main`, it also publishes a successful build
through GitHub Pages, using GitHub's Pages environment and deployment action; no
generated branch or local credentials are involved. The repository's Pages source
must be set to **GitHub Actions**.

The published site is
[fireflyframework.github.io/firefly-weave](https://fireflyframework.github.io/firefly-weave/).
A local build proves that the site renders. Only a successful Pages deployment
followed by a check of the live address proves that readers can reach it.

## When a check fails

| What you see | Why | What to do |
| --- | --- | --- |
| `The following pages exist in the docs directory, but are not included in the "nav" configuration` | A new page is missing from the navigation | Add it to `nav:` in `mkdocs.yml` |
| `Documentation link does not exist: docs/PAGE.md: TARGET` | A relative link points to a file that does not exist | Fix the path; it is relative to the page that contains the link |
| `Documentation link points outside public sources: …` | A link targets a private, hidden, or out-of-repository file | Remove the link; that material is never published |
| `Doc file 'PAGE.md' contains a link 'OTHER.md#ANCHOR', but the doc 'OTHER.md' does not contain an anchor '#ANCHOR'` | A heading was renamed, or the link has a typo | Link to the current heading, or restore the heading if other pages depend on it |
| `Aborted with N warnings in strict mode!` | At least one warning above it | Fix every listed warning, then build again |
| `check_docs.py` reports `missing`, `anchor`, or `private/outside` for a page | The same link problems, checked the way GitHub renders the Markdown | Open the named page and fix its links; the report names the page, not the link |
| `check_docs.py` reports `unreachable` | No chain of links leads from the root `README.md` to the page | Link to the page from a related guide |
| `check_docs.py` reports `svg-structure` | A diagram lacks `role="img"`, `<title>`, `<desc>`, or a `viewBox`, or contains a script, `foreignObject`, or embedded image | Fix the SVG as described in [visual assets](../visual-assets.md#draw-or-change-a-technical-diagram) |
| `source_coverage.py` reports `missing-header` | A new file lacks the Apache-2.0 header | Copy the header from a neighboring file; see [source documentation](source-documentation.md) |

## Next steps

- [Source documentation and attribution](source-documentation.md): license
  headers, module docstrings, and the inventory for files that cannot carry
  comments.
- [Visual assets](../visual-assets.md): the brand assets, and how to draw and check
  a diagram.
- [Contributing](../../CONTRIBUTING.md): the complete project checks.
