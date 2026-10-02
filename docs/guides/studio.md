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

# Firefly Weave Studio

Studio is the local visual workspace for Weave workflow definitions. It uses the
same compiler, API authorization and durable runtime as the CLI and SDK. The
browser connects to a paired Python host; API credentials remain in that host.

![Studio and runtime boundaries](../diagrams/studio-and-runtime.svg)

## Choose what you want to do

| Your goal | What must be running | Start here |
| --- | --- | --- |
| Open Studio as a native desktop app | A matching desktop build for your operating system | [Build and use the desktop app](desktop.md) |
| Draw or import a process on your computer | Studio's local host | [Install and launch Studio](#install-the-alpha5-browser-application) |
| Save and run processes on your laptop | Studio plus a separate local Weave platform | [Start the platform](local-platform.md), then [connect Studio](#connect-an-authorized-api-profile) |
| Work with your team's remote platform | Studio on your computer; your team's API remains remote | [Get your connection details](connect-to-api.md), then [connect Studio](#connect-an-authorized-api-profile) |
| Approve a task or inspect a run | Connected Studio with the relevant Weave grants | [Human work](#complete-human-work) and [execution lifecycle](#save-publish-activate-and-run) |

**Studio is a client.** Launching it starts a small local host and the editor. It
does not provision PostgreSQL, an identity provider, workers, or a remote platform.
Closing Studio does not stop executions already running on the platform.

**Not connected to a platform** means Studio is working with files on your
computer. You can create, import, edit, and validate workflow definitions. Connect
when you want to save them to a shared platform, run processes, or handle tasks.
This status does not mean your computer has lost its internet connection.

## Install the alpha5 browser application

Alpha5 includes the Studio host. Its browser assets are an optional, matching
release bundle; the Python wheel does not embed the Angular application. Alpha4
does not include Studio. Download all files from the same
[v0.1.0a5 release](https://github.com/fireflyframework/firefly-weave/releases/tag/v0.1.0a5).
The CLI installer does not automatically fetch or install the Studio ZIP.

Use Python 3.12 or newer. The alpha5 CLI installer includes the Studio host
and authentication dependencies, but downloads no browser ZIP automatically.
These Bash/Zsh commands need neither Node nor a source checkout:

```bash
# Install the pinned CLI into its isolated installation directory.
# Review the installer first using the download-and-inspect alternative in Installation.
curl --fail --location https://github.com/fireflyframework/firefly-weave/releases/download/v0.1.0a5/install.sh \
  | sh -s -- --version v0.1.0a5
# Make the installed command available in this terminal.
export PATH="$HOME/.local/bin:$PATH"
# Check that the host version matches the browser bundle you will install.
weave --version

# Keep the downloaded optional browser bundle and its digest together.
mkdir weave-studio-alpha5
cd weave-studio-alpha5
curl --fail --location --remote-name https://github.com/fireflyframework/firefly-weave/releases/download/v0.1.0a5/firefly-weave-studio-0.1.0a5.zip
curl --fail --location --remote-name https://github.com/fireflyframework/firefly-weave/releases/download/v0.1.0a5/firefly-weave-studio-0.1.0a5.zip.sha256
# Verify the bundle on macOS. Stop if the check fails.
shasum -a 256 --check firefly-weave-studio-0.1.0a5.zip.sha256
# On Linux, use sha256sum --check in place of shasum -a 256 --check.

# Install only the bundle whose version and verified digest match this host.
weave studio install --bundle firefly-weave-studio-0.1.0a5.zip \
  --sha256 "$(awk '{print $1}' firefly-weave-studio-0.1.0a5.zip.sha256)"
# Start the local host without opening a browser automatically.
weave studio --no-browser
```

Open the printed loopback address, enter the terminal pairing code, and select
**Connect to Studio**. Leave the terminal running; Ctrl+C stops this host.
Pairing codes expire and are one-use. Restart for a new code if needed. Tokens and
pairing codes do not belong in URLs. After closing the terminal, run `weave studio` again to launch the installed bundle.

An advanced wheel-only installation must explicitly select the `studio` extra
for this host, or `client` for API-only SDK use; see [installation](../installation.md). For the durable platform, follow [local platform setup](local-platform.md)
or the [remote deployment guide](../operations/remote-deployment.md). Installing
Studio neither provisions nor migrates a platform.

## Start from source

Contributors can build the matching browser assets themselves. For the alpha5
release, check out `v0.1.0a5`; development `main` can differ from released assets.

From the repository root, install the development dependencies first. Node is
required only for building the browser application.
Angular 22.2.1 is pinned in the application and `package-lock.json` fixes the
complete dependency graph.

```bash
# Create the locked Python environment, including optional features and docs.
uv sync --locked --all-extras --group docs

# Enter the browser application directory.
cd studio

# Install exactly the dependency versions recorded in the lockfile.
npm ci

# Compile the production application; this does not publish it.
npm run build

# Return to the repository root to use the Python development environment.
cd ..

# Serve only the freshly built browser assets on loopback.
.venv/bin/weave studio --assets studio/dist/studio/browser --no-browser
```

Open the printed loopback address, enter the terminal pairing code, and select
**Connect to Studio**. The pairing code is one-use and expires. Restart the host
for a new code if pairing fails. Keep the terminal running while using Studio;
Ctrl+C stops the host. Neither pairing codes nor account tokens belong in URLs.

A source build can also install its own matching generated Studio bundle by digest:

```bash
# Install the bundle generated for this exact Weave version.
# Replace both placeholders with the real bundle path and verified SHA-256.
.venv/bin/weave studio install --bundle /path/to/weave-studio.zip \
  --sha256 YOUR_VERIFIED_SHA256

# Launch the installed bundle; Node and --assets are no longer required.
.venv/bin/weave studio --no-browser
```

The installer checks bundle/version/digest compatibility. Do not mix a source host
with assets generated for a different version.

## Draw and validate a workflow locally

Keep the [step-by-step property reference](studio-step-reference.md) open when
choosing a step. It explains integration calls, expressions, approvals, and the
difference between step properties and workflow-wide settings.

1. Open **Home**, then select **New workflow** or import an existing YAML/JSON
   definition. Connected Home shows authorized task and execution previews;
   disconnected Home explains what can be done on this computer. Opening a workflow centers its complete
   graph. Pan/zoom stays under your control while editing.
2. Select a palette step to insert after the selected step. Dragging onto a `+`
   insertion target inserts at that exact sequence or branch position.
3. Select the step and edit its configuration in **Inspector**. Actions refer to
   registered action versions; the placeholder `your-action@1.0.0` must be replaced
   with a real reference before catalog compilation can succeed.
4. Add a **Decision** for explicit switch cases/default or **Parallel** for named
   independent branches. These are structured Weave nodes, not BPMN imports.
5. Select **Validate** to call the Python compiler. Offline results explicitly
   indicate that catalog checks remain pending. Passing local syntax/schema
   checks alone does not prove that actions or worker releases are available.
6. Use **Export** for the definition and separate `.layout.json` sidecar. The
   sidecar carries positions, viewport and definition digest; it is not execution
   input and is never added to the workflow schema.

Start and End are fixed visual boundaries, including an empty Start → End flow;
they are never added as synthetic execution steps. The inspector opens a
Property/Value table with typed values and literal/reference selectors. Edit fields
and select **Apply configuration**; invalid values keep the existing step intact.
Nested values expand into rows. **Advanced JSON** remains optional for complex
configuration.

Moving a node with its handle changes layout only. Moving execution order uses
**Move to branch and position** in the inspector or an output port followed by an
insertion target. A decision/parallel group retains ownership of its nested steps.
Cycles and moving a group inside itself are rejected. Deleting a populated group
requires explicitly moving/removing its children first; referenced steps cannot
be deleted while another expression points to them.

A drop onto blank canvas creates an **unplaced** draft step. Place it with the
inspector's target picker or delete it before validation, export or publication.
Unplaced steps are visibly excluded from the execution definition.

**Source** provides YAML and JSON editing. **Apply source** parses the edited
buffer; a malformed or unsupported definition retains that exact buffer and the
last valid graph becomes read-only. Repair the source before resuming graph
editing. YAML comments are preserved through supported structural edits; converting
YAML with comments to JSON asks for explicit normalization. **Outline** offers a
textual execution tree and branch labels.

Undo/redo applies to local authoring and layout gestures. It never reverses a
published version, run start, human decision or email submission. Arrow keys select
steps, Delete removes a selected step, Escape cancels a gesture, and Ctrl/Command Z
and Shift Z undo/redo. Ctrl/Command S saves a connected draft. Every palette
insertion and structural move has a keyboard-operated button or target picker.

Desktop is the primary authoring surface. Smaller screens expose the inspector as
an overlay and prioritize source, lists and task completion. Authoring state is
memory-only; export or save it before refreshing. Studio does not write tokens,
unredacted task/mail content or workflow recovery data to browser storage.

## Connect an authorized API profile

### Use the connection assistant

You can connect from Studio without entering API commands in a terminal. Obtain
your operator's approved OAuth **login configuration** first. It contains the API
origin, provider ID, issuer, OAuth client, and account label; it is not an access
token or a CIAM password.

1. Open **Settings → Connect a workspace**. Home also links to connection setup.
2. Select **Import login configuration** and choose the approved JSON file.
   Alternatively, expand **Enter connection details** to enter the values your
   operator supplied. Review the destination API and
   identity-provider information before confirming it. A local development API
   can use loopback HTTP; remote APIs require HTTPS.
3. Check **I trust this API target and identity provider**, select
   **Save reviewed connection**, then **Sign in**. Studio selects the supported
   OAuth flow. For a device flow, open the displayed verification address in your
   regular browser and enter the displayed code. For PKCE, follow the supplied
   authorization link and complete sign-in in your regular browser.
4. Keep Studio open until sign-in finishes. You can cancel a pending login; the
   host cancels its owned authentication task and callback listener. An expired
   or denied login requires a deliberate new attempt.
5. Select **Check connection and discover workspaces**. Studio calls the identity API using the host-held login
   and shows the workspaces that identity may access.
6. Choose your tenant, project, and environment in **Workspace and environment**,
   then select **Change workspace**. Confirm the selected destination in the top
   bar before saving, publishing, or starting a run.

The import area accepts one JSON file up to 64 KiB and shows its filename and
review status. Use **Replace login configuration** to choose another file. You
can use the keyboard-operated file button instead of dragging. Changing the
configuration clears its trust confirmation; review and save the replacement
before signing in. While Save is pending, configuration controls are locked and
dropped replacement files are ignored. Invalid files show a recovery message
without displaying their contents.

The assistant configures this local host session. It does not create a server or
give your account new permissions. Configuration is not saved in browser storage;
import it again after restarting, or use the reusable CLI profile below. Tokens
are stored through the approved native credential store and remain outside the
editor. Linux requires an available system keyring for this assisted flow.

If the desktop app cannot open the sign-in link, select **Copy sign-in address**
and paste it into your regular browser. The address and device code are also
selectable text. Return to Studio to see completion; do not paste an access token
or refresh token into the editor. A profile that uses CLI-managed credentials can
still be checked here, but its login must be refreshed through the CLI or replaced
with a reviewed connection using the native credential store.

A successful CIAM sign-in can still produce a Weave access error: the verified
identity needs an active local link and suitable grants. Follow
[People and access](people-and-access.md) for that separate onboarding step.

### Reuse a CLI login and saved profile

If you already use the CLI, authenticate using your operator's OAuth configuration:

```bash
# Uses your configured CIAM and saves credentials in the native credential store.
weave auth login --auth-config /absolute/path/to/oauth.json
```

Create a Studio profile from the **same** login configuration. This avoids typing
the API origin twice or accidentally selecting a different server:

```bash
# Generate a non-secret profile; an existing file is never overwritten.
weave studio configure \
  --name "My team" \
  --auth-config /absolute/path/to/oauth.json \
  --output ./studio-profile.json
```

Expected: `Studio profile saved`, followed by its absolute path. This command
does not authenticate, create users, or grant permissions. It takes the API origin
from the login configuration and stores an absolute reference to that file.
Optional `--tenant`, `--project`, and `--environment` preselect a complete scope;
otherwise choose it in Studio. If you logged in using a private credential file,
pass the same `--credential-store file --credential-file /absolute/path/to/session.json`
to `configure` as you used for `auth login`.

You can also create or inspect the profile yourself. Its non-secret structure is:

```json
{
  "name": "Development",
  "base_url": "https://weave.example.com",
  "auth_config": "/absolute/path/to/oauth.json",
  "credential_store": "native"
}
```

Replace the example origin with your API origin. The OAuth configuration's target
must match it. Use absolute file paths so launch does not depend on your working
directory. Optional `tenant_id`, `project_id`, and `environment_id` fields select
an initial workspace; otherwise use **Settings → Authorized workspaces** after
connecting. Environment selection also requires its project and tenant IDs.
Local development APIs may use an explicit loopback HTTP origin; remote APIs
require HTTPS. Tokens and client secrets never belong in this profile JSON.

Then launch with that profile:

```bash
# Use your operator-provided, non-secret profile JSON file.
# Its auth_config points to the supported CLI OAuth configuration.
weave studio --profile /path/to/studio-profile.json --no-browser
```

The profile defines the API server and initial tenant/project/environment scope.
Use the supported CLI authentication flow before launch. A CLI profile is an
explicit identity choice; it does not grant administrator privileges. The server
continues to check local identity links, grants and current membership.

**Settings → Authorized workspaces** uses the identity discovery API. Only visible
resources appear. Changing scope requires host validation and clears the previous
scope's in-memory sensitive context. The server/environment remain visible in the
top bar. Permission-gated controls are hidden or disabled; API denial is still
authoritative if a grant is revoked while the page is open.

### Switch between local and remote platforms

Keep one profile per server. For example, `local-studio.json` references a login
configuration targeting your local API, while `team-studio.json` references your
team's HTTPS API. Export or save unfinished work, stop the local Studio host with
Ctrl+C, and launch it again with the other `--profile`. Pair the new session and
confirm the server and environment in the top bar before making changes.

The editor does not deploy the remote server or tunnel to it. Your computer must
be able to reach the configured origin, including any required VPN. The local
Studio address remains `http://127.0.0.1:8766`; the selected remote API address is
shown separately. `--port` changes the local Studio port if it is already in use.

### Move between Studio, the CLI, and the SDK

All three clients use the same definitions and API operations. Export YAML from
Studio to review it in Git or import an existing CLI-authored YAML/JSON file into
Home. A saved server draft is a shared resource; a locally opened file is not
uploaded until you explicitly save it. Layout sidecars affect presentation only.

| Studio action | Equivalent workflow outside Studio |
| --- | --- |
| Import or Export | Edit a YAML/JSON definition in your project |
| Validate | `weave workflow validate` or the Python compiler API |
| Save draft / Publish | Definition operations in the [CLI tutorial](cli-tutorial.md) or [SDK tutorial](sdk-tutorial.md) |
| Activate / Start execution | Activation and run API operations with the same scope and grants |
| Claim / Complete task | [Human task API, CLI, and SDK](human-tasks.md) |
| Inspect / Archive execution | [Run discovery and lifecycle operations](execution-management.md) |

## Save, publish, activate and run

These are separate lifecycle operations:

- **Save draft** writes the current definition using the acknowledged revision.
  “Saved to API” appears only after the response. A revision conflict preserves
  your buffer and opens a local/server comparison; keep editing or save as a new
  draft rather than overwrite a colleague's revision.
- **Publish** validates the current source and creates an immutable version through
  the publication API. The connected compiler performs authoritative catalog
  checks; local validation is not publication authority.
- **Activate** binds a published artifact to the selected environment. Review the
  environment and provide authorized connection/worker release bindings. The API
  rejects missing or invalid bindings and computes readiness.
- **Start run** chooses an authorized activation and accepts explicit JSON input,
  plus optional business and correlation keys. Each click creates a distinct
  execution identity; the same business key can group multiple executions.
  **Runs** filters on exact business key, correlation key and runtime status, and
  resets pagination when filters change. It reads
  the version-pinned run and bounded history. Manual pause/resume needs separate
  capabilities and an audit reason; incident state remains independent.
- **Archive execution** hides a terminal run from ordinary discovery while keeping
  history. **Include archived executions** exposes archived entries and **Restore
  execution** returns them to discovery. **Purge execution content** is a separate
  manager operation: it requires an archived terminal run, an audit reason and
  typing its exact run ID. The API refuses purge while required durable references
  or active leases remain; audit receipts are retained. Purge is never automatic.
- **Simulate** creates a real debug session from a fully compiled artifact and
  explicit input/mocks. Next/continue inspect simulated boundaries without sending
  actual external effects.

A network failure after a mutation can mean an unknown outcome. Studio retains
its exact request identity and payload and offers explicit reconciliation. It does
not issue an automatic new approval, run or email operation. Source remains in
memory when validation/save fails. Export it before closing an offline browser.

## Complete human work

For onboarding people and assigning editor, operator, viewer, or administrator
permissions, see [People and access](people-and-access.md). Login remains with
your configured CIAM; Weave controls scoped access to workflows and executions.

**My Tasks** lists authorized human tasks and opens their business context, form,
allowed decisions, due time and expiry. Due time marks attention; it does not
synthesize an approval. Use the server-backed status filter to find available, claimed, completed, expired
or cancelled work. Open a detail for its bounded audit history. Claim an available
task, complete the schema-driven fields,
and submit an allowed decision. Claim/release/complete carry expected revisions and
stable idempotency identities. The displayed decision actor is the authenticated
principal, never a selectable impersonation identity.

Only the current claimant with current task eligibility can complete it. A stale
revision or revoked authorization leaves the form intact and asks you to reload.
Completed, cancelled and expired tasks remain read-only. The task API checks the
same rules for Studio, CLI and product integrations.

## Read and reply to email

**Email** loads authorized conversations and renders plain text safely. It does
not execute received HTML. A reply references an actual inbound message in the
conversation and its configured connection revision. **Queue reply** records the
submission. **Send queued email** is the separate explicit transport action.

The UI distinguishes queued, attempting, SMTP accepted, rejected and unknown.
SMTP accepted is transport acceptance, not proof of recipient delivery or reading.
An unknown result requires reconciliation with the operator before another send;
there is no automatic resend. **Check submission status** reads the existing
receipt. Sending a reply does not complete a human task.

Reply/form buffers remain only in the current browser session. They are not
server-persisted drafts and do not survive refresh.

## Validate a source contribution

```bash
# Run these commands from studio/.
npm run check          # Strict TypeScript checking.
npm run format:check   # Source formatting check.
npm test               # Structured adapter and transport unit tests.
npm run build          # Angular template checks and production bundle.

# Install the owned browser-test runtime, then exercise pointer and keyboard UI.
npx playwright install chromium
npm run test:browser
```

The browser suite includes a real loopback PyFly-host pairing/CSP/local-validation
test when the repository Python `.venv` is available. Connected API fixtures test
request shapes and failure handling; they are separate from database/mail transport
integration evidence. Tests use owned fixtures and never a production service.

## When a step does not work

| What you see | What to check next |
| --- | --- |
| Pairing code expired or was already used | Restart the local Studio host and pair with its new code |
| Not connected in the top bar | Open Settings and complete the connection assistant, or launch with an authenticated profile |
| No authorized workspace appears | Confirm your identity link and Weave grants; a CIAM login alone does not grant access |
| HTTP 401 while connected | Sign in again through the assistant, or refresh the CLI-managed login with its same configuration |
| Native credential store unavailable | Enable the operating system keyring, or use the explicitly configured CLI file-store profile |
| Sign-in cancelled, denied, or expired | Read the status, correct the configuration if necessary, and deliberately start a new sign-in |
| HTTP 403 for one operation | Confirm that your current role permits that operation in the selected scope |
| An action reference is not found | Choose a published action version from this project's catalog |
| A valid workflow cannot activate | Check its connection slots, worker release bindings, and human assignment bindings |
| A saved draft changed on the server | Use the revision-conflict comparison; keep or export your local work before reloading |
| A request's outcome is unknown | Reconcile the existing request before issuing a new operation |

For server startup, see [local platform troubleshooting](local-platform.md).
For deployment and worker failures, see [operational troubleshooting](../operations/troubleshooting.md).
