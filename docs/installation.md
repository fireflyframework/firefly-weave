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

# Install the Weave CLI

**Goal:** type `weave` from any directory to write workflows or call an existing
Weave API. Installing the CLI does not start a server, database, or worker.

The installer includes local authoring, the API client, authentication, OpenAPI
import, and the Studio host dependencies. Install the optional matching browser
bundle separately using the [Studio guide](guides/studio.md#install-the-alpha5-browser-application). It keeps these in a separate Python environment, so
your application's packages stay independent.

## Choose the installation path

**Recommended:** install the pinned **v0.1.0a5 alpha** below. You do not need Git,
a source checkout, Docker, or `sudo`. An alpha is a preview release; use it to
evaluate the platform and check the [current limits](capabilities.md).

| Your situation | Start here |
| --- | --- |
| You want the visual browser workspace | [Install Studio](guides/studio.md#install-the-alpha5-browser-application) |
| You want a native desktop app | [Desktop installation](guides/desktop.md) |
| You want to use the CLI or API | [Install the release](#install-a-release), then [check the result](#check-the-result) |
| You contribute code or need unreleased changes | [Install the current source](#install-the-current-source) |
| You already have the CLI | [Upgrade or select another version](#installation-locations-and-upgrades) |

The original `v0.1.0a1` release predates this installer. Use `v0.1.0a2` or a later
release containing CLI installer assets.

![Verified download, isolated installation, and command activation](diagrams/cli-installation.svg)

The existing `weave` command changes only after the new environment passes its
checks. [Open the diagram](diagrams/cli-installation.svg).

## Before you start

Use macOS, Linux, or a Linux shell under Windows (WSL), with Python 3.12 or newer
and its `venv`/`ensurepip` support. Python 3.12 is the reference version used in
project checks. Native Windows installation is not supported by the shell script.
You also need `curl` and internet access to download dependencies, including the
pinned PyFly wheel from GitHub. You do not need Docker or a separately installed PyFly.

```sh
# Check the interpreter used by the installer; Python 3.12+ is required.
python3 --version
# Confirm the download tool is available before using the install command.
curl --version
```

If that interpreter is older than 3.12, select an installed compatible interpreter
with `WEAVE_INSTALL_PYTHON`, or use the source route below to let `uv` select 3.12.
On Linux, your distribution may package `venv` separately.

## Install a release

**Step 1:** check Python using the command above. **Step 2:** copy this complete
block into Bash or Zsh. It downloads the installer from the release and selects
that same version explicitly:

```sh
(
  # Stop if downloading the installer fails.
  set -o pipefail
  curl --proto '=https' --tlsv1.2 -fsSL \
    https://github.com/fireflyframework/firefly-weave/releases/download/v0.1.0a5/install.sh \
    | sh -s -- --version v0.1.0a5
)
```

The installer downloads the release manifest, package, and dependency list;
checks their SHA-256 hashes; creates an isolated Python environment; and checks
the installed command before making it available. Expect progress messages
followed by the installed version and command path. This may take several
minutes on the first install. No services start and no cloud resources are created.

`set -o pipefail` ensures a failed download produces a failed command. If your
organization requires inspecting scripts before running them, use this equivalent
route instead:

```sh
curl --proto '=https' --tlsv1.2 -fsSL \
  https://github.com/fireflyframework/firefly-weave/releases/download/v0.1.0a5/install.sh \
  -o weave-install.sh
```

After the download succeeds, inspect `weave-install.sh` in your editor, then run:

```sh
sh weave-install.sh --version v0.1.0a5
```

HTTPS and checksums protect the artifact path; checksums are not an independent
signature of the release publisher. Only run installers from a source you trust.
Keep this file if you want to use it again for upgrades or removal.

**Step 3:** continue to the command checks below before starting a tutorial.

## Check the result

If your shell cannot find `weave`, add the installer's printed bin directory to
PATH. For the default location in the current Bash or Zsh session:

```sh
# Make the installed command available in this shell.
export PATH="$HOME/.local/bin:$PATH"
weave --version
# Show command groups and examples without starting a server.
weave --help
weave version --output json
weave docs platform
```

Expected for the pinned installation: version `0.1.0a5`, branded help with command
groups and examples, and one JSON object with `version`, `apiVersion`, and `irVersion`. The banner appears only in
root help; it never contaminates command results or JSON output. For help on one
operation, run `weave help workflow compile` or `weave workflow compile --help`.

For a ready-to-run example, use `weave init my-first-workflow`, enter that
directory, and follow its generated `README.md`. Use `weave docs platform` when
you are ready to start the API and its dependencies. `weave docs` prints links
without opening a browser; add `--open` when you want to visit a guide.

To keep PATH across terminal sessions, add that export to your own shell startup
file. The installer deliberately leaves shell configuration under your control.

## Installation locations and upgrades

| Item | Default location |
| --- | --- |
| Isolated installations and receipts | `~/.local/share/firefly-weave` |
| Command | `~/.local/bin/weave` |

Use `--install-dir` and `--bin-dir` to select different locations. Quote paths
that contain spaces. Use the same locations for future installs and uninstall.
To choose Python explicitly, set `WEAVE_INSTALL_PYTHON` to its executable path.
If you downloaded the installer file, run `sh weave-install.sh --help` for all
supported options.

An upgrade is another installation using the desired release or prepared local
artifacts. The installer creates a new environment, checks dependencies and the
CLI, then switches its managed command. If installation fails, the previous
command remains selected. It refuses to replace an unrelated `weave` executable.
Do not remove or move the selected Python interpreter: the environment may rely
on it. Keep old environments until you no longer need them.

### Select a version or release channel

The explicit `--version v0.1.0a5` in the recommended command makes installation
repeatable. To upgrade, use the installer URL and `--version` value from the same
new release shown on [GitHub Releases](https://github.com/fireflyframework/firefly-weave/releases).
The tag includes a leading `v`; `weave --version` shows the package version without it.

If you retained `weave-install.sh`, these commands select a channel instead:

| Command | What it selects |
| --- | --- |
| `sh weave-install.sh --version v0.1.0a5` | Exactly this documented alpha |
| `sh weave-install.sh --prerelease` | The most recently published release, including alpha previews |
| `sh weave-install.sh` | The latest stable release only |

Weave currently publishes alpha releases. A stable-only request can therefore
report that no release was found; use the pinned command above. Channel selection
is convenient, but exact versions make team setup and troubleshooting repeatable.
After any upgrade, repeat `weave --version` and the quickstart's validation step.

## Remove the command

If you retained the downloaded installer, run:

```sh
sh weave-install.sh --uninstall
```

If you used the curl installation, use the same published installer with the
removal option:

```sh
(
  # Stop if downloading the installer fails.
  set -o pipefail
  curl --proto '=https' --tlsv1.2 -fsSL \
    https://github.com/fireflyframework/firefly-weave/releases/download/v0.1.0a5/install.sh \
    | sh -s -- --uninstall
)
```

Supply `--install-dir` and `--bin-dir` again if you selected custom locations.
This removes only the installer's managed command. Retained installations, your
workflow files, and authentication credentials are separate; review them before
manually removing anything. Use `weave auth logout` with your authentication
configuration before uninstalling if you also want to clear the CLI's saved login.

## If installation stops

| Symptom | What to do |
| --- | --- |
| Python version or `venv` error | Select Python 3.12+ with `WEAVE_INSTALL_PYTHON`; install your distribution's venv support |
| No stable release | Run the pinned `v0.1.0a5` command above or explicitly choose `--prerelease` |
| Missing installer assets | The selected release is too old; do not mix assets from different releases |
| Checksum mismatch | Stop and obtain the complete assets again from the same trusted release |
| Existing unrelated `weave` | Choose a different `--bin-dir`; inspect the old command before replacing it yourself |
| Dependency download fails | Check access to PyPI and GitHub; the previous installed command remains available |
| `weave: command not found` | Add the printed bin directory to PATH or invoke the full printed command path |

## Install the current source

This advanced route follows **unreleased development source on `main`**. Its
version and behavior can differ from the pinned release above. Use it for
contributing or evaluating changes, and follow the documentation in that same
checkout. To use the documented release, prefer the published installer.

You need Git and [uv](https://docs.astral.sh/uv/getting-started/installation/) for
this route. Clone once, then run all commands from the repository root:

```sh
git clone https://github.com/fireflyframework/firefly-weave.git
cd firefly-weave
uv sync --locked --python 3.12
mkdir -p .local
uv run --locked --no-editable python scripts/prepare_release.py \
  --output .local/cli-release
WEAVE_INSTALL_PYTHON="$PWD/.venv/bin/python" sh install.sh \
  --from-release .local/cli-release/artifacts
```

Already cloned? Skip the first two commands and enter your checkout. The prepare
step builds the package once and exports exact dependency versions and hashes.
It does not start any service. Its output directory must be new: on a later build,
choose a different directory and pass that same directory's `artifacts` child to
the installer. Keep the build output if you need to reinstall that exact build.

If you use `UV_PROJECT_ENVIRONMENT`, select that environment's `bin/python`
instead of `.venv/bin/python`. The installer prints the installed version,
command location, and any PATH instruction needed for your shell.

## Next: choose one task

- [Try your first workflow](quickstart.md): no server or credentials needed.
- [Connect to an existing API](guides/connect-to-api.md): use the URL and access your team provides.
- [Run Weave locally](guides/standalone.md): start the API, PostgreSQL, and Keycloak for development.
