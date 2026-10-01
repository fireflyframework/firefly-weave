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

The website renders the same public Markdown that readers browse in GitHub.
[MkDocs](https://www.mkdocs.org/) and
[Material for MkDocs](https://squidfunk.github.io/mkdocs-material/) provide navigation,
search, code copying, and a responsive light/dark reading layout. Official Weave
artwork and local system fonts keep the site aligned with the project identity.

## Preview a change

From a source checkout, install [uv](https://docs.astral.sh/uv/) and Python 3.12 or
newer, then run:

```bash
uv sync --locked --group docs --extra openapi
uv run --locked --group docs --extra openapi mkdocs serve --dev-addr 127.0.0.1:8000
```

Open the local address printed by MkDocs. Stop the server with Ctrl-C.
The build installs the package and its OpenAPI extra to generate the complete API
reference. It does not start the API, database, identity service, or workers.
If another process owns the checkout's Python environment, set
`UV_PROJECT_ENVIRONMENT` to a separate local directory before running either
command. Restart the preview after changing the build hook.

Run the publication check before submitting:

```bash
uv run --locked --group docs --extra openapi mkdocs build --strict
python3 scripts/check_docs.py
```

The rendered site is written to `build/docs-site/`. This directory is generated
and ignored by Git. Do not commit it. The standard project check also builds the
site; see [contributing](../../CONTRIBUTING.md) for the complete validation command.

## Add or update a page

1. Edit the existing Markdown under `docs/`, or add a new public page there.
2. Link it from a relevant guide and add it to the explicit navigation in
   [mkdocs.yml](../../mkdocs.yml).
3. Keep links relative to the source file, including links to code or project
   files outside `docs/`. Use normal Markdown headings, links, images, and tables.
4. Check the rendered page at desktop and mobile widths, in both appearances.
   Try search, keyboard navigation, code copying, and the page's diagram links.
5. Build with `--strict`; missing pages, internal links, and heading anchors fail
   the build. The separate Markdown check validates the GitHub source navigation.

Do not wrap Markdown captions or links in raw HTML. This can stop Markdown link
processing and produce a page that appears correct in one renderer but breaks in
another. Refer to [visual assets](../visual-assets.md) for editable diagrams and
approved logos. Preserve readable contrasts and image descriptions.

## How repository links become site links

The [site hook](../../scripts/docs_hook.py) rewrites parsed links before MkDocs
validates them. Links between documentation pages and approved artwork become
site links. Links to source code and deployment templates open their repository
location. Code examples retain their literal contents.

The hook includes a fixed list of root project documents, Kubernetes template
notes, and connector fixture notes as virtual pages; it does not copy the
repository into the site. It includes only explicitly approved official artwork
from `assets/`. Private plans, local settings, hidden files, and the internal
implementation status are excluded. Add any new public root document explicitly
to the hook and navigation, then test its links.

Set `WEAVE_DOCS_REF` to a release tag or commit to point source links at that
revision when building a release snapshot. The normal site follows `main`.
This setting changes source links; it does not check out a different revision or
create a version switcher. Check out the intended revision before building.

## Publish through GitHub Pages

The [documentation workflow](../../.github/workflows/docs.yml) builds the locked
site on pull requests and publishes successful builds from `main` through GitHub
Pages. Its deployment job uses GitHub's Pages environment and deployment action;
no generated branch or local credentials are needed. The repository's Pages
source must be **GitHub Actions**.

The canonical address is
[fireflyframework.github.io/firefly-weave](https://fireflyframework.github.io/firefly-weave/).
A build passing locally proves the site renders; a successful Pages deployment
and a live HTTP check establish that the published site is available.
