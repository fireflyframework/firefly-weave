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

# File connector workers

The independently operated `workers/files` package provides FTP, explicit FTPS, SFTP, Microsoft Graph drives (SharePoint and OneDrive), and Google Drive actions. Binary data travels through the [file API](files.md) as bounded chunks; workflow state contains a `weave/file` reference with its filename, content type, size and SHA-256 digest.

## Operations and catalog

All connectors are version `1.0.0`. Connector names are `weave-ftp`, `weave-ftps`, `weave-sftp`, `weave-microsoft-drive`, and `weave-google-drive`. An action is named `<connector>-<operation>@1.0.0`; its worker capability is `<connector>.<operation>@1.0.0`.

| Operation | Input | Result |
| --- | --- | --- |
| `list` | Optional folder `path` or cloud `itemId`, `limit` (1–100), optional `cursor` | `{items, nextCursor}` |
| `read` | File `path` or cloud `itemId` | File metadata |
| `download` | File `path` or cloud `itemId` | Verified `weave/file` reference |
| `write` | `file` reference and `destination`; cloud also requires `name` | Created file metadata |
| `move` | `path` or cloud `itemId`, `destination`; cloud also requires `name` | Moved file metadata |
| `delete` | File `path` or cloud `itemId` | `{deleted: true}` |

Metadata has `id`, `name`, `isFolder`, and available `sizeBytes`, `contentType`, `modifiedAt`, `version`. `read` reads metadata; it never returns binary data in JSON. FTP/SFTP destinations are full relative file paths. Cloud destinations are parent folder IDs. SFTP writes refuse existing destinations; FTP/FTPS destination behavior requires the explicit policy described below. Graph writes request conflict failure; Google Drive creates a new file and permits duplicate names according to Drive semantics. Deletion is file-only: recursive folder deletion is unavailable.

```sh
uv sync --project workers/files --locked
uv run --project workers/files weave-files-worker --catalog > /tmp/files-catalog.json
uv run --project workers/files weave-files-worker --release-manifest > /tmp/files-release-capabilities.json
```

The catalog contains five Connector definitions, thirty Action definitions, task capabilities and adapter identities. Follow [Complete your first transfer](#complete-your-first-transfer) to admit the image, publish these definitions, and bind a run. Each action has a 300-second bound and one attempt. Writes, moves and deletions have `non_idempotent` side effects; an uncertain result must be investigated before an operator retries.

## Connections and authority

Connections use the existing secret-handle system. The API validates public configuration; only a worker holding the current task lease obtains its pinned connection metadata and short-lived credential lease. Each config explicitly lists permitted `operations`.

| Connector | Required public config | Secret slot | Allowed destination |
| --- | --- | --- | --- |
| FTP / FTPS | `host`, `port`, `username`, `rootPath: /`, `serverRootIsolated: true`, `operations`; optional `destinationPolicy` | `password` | `ftp://host:port` / `ftps://host:port` |
| SFTP | `host`, `port`, `username`, `rootPath: /`, `serverRootIsolated: true`, OpenSSH `hostKey`, `operations` | `password` | `sftp://host:port` |
| Microsoft drive | `driveId`, `rootFolderId`, `operations` | `accessToken` | `https://graph.microsoft.com` |
| Google Drive | `rootFolderId`, `operations` | `accessToken` | `https://www.googleapis.com` |

Example SFTP connection configuration:

```yaml
host: files.example.com
port: 22
username: weave
# First isolate this account to the approved folder on the server.
# That folder appears as / when this account signs in.
rootPath: /
serverRootIsolated: true
hostKey: ssh-ed25519 REPLACE_WITH_THE_VERIFIED_SERVER_PUBLIC_KEY
operations: [list, read, download]
```

The host key is public identity evidence; obtain it through the operator's trusted channel. Unknown or mismatched SSH host keys fail closed. Passwords and OAuth access tokens belong in credential providers, never in these files. Cloud token scope and renewal remain the credential provider's responsibility; this package does not implement an interactive OAuth login or store refresh tokens.

FTP, FTPS, and SFTP require an account whose root is isolated by the server. Configure an operating-system chroot or an equivalent boundary that also prevents symbolic links from reaching outside the approved folder. A virtual folder prefix alone is insufficient. Set `rootPath: /` and `serverRootIsolated: true` only after the server administrator has established this boundary. The worker cannot verify that administrator assertion over the protocol. A narrower client-side path is not a security boundary: another writer could change directories between checking a path and opening it.

SFTP additionally checks path components and rejects observed symbolic links. All three connectors reject parent traversal, absolute operation paths, backslashes, and control characters. Cleartext FTP is disabled unless the worker operator explicitly enables it. FTPS upgrades the control connection before login and encrypts its data connections; certificate and hostname verification remain enabled.

### Choose safe destination behavior

Use SFTP when a transfer must never replace a concurrently created destination. SFTP writes use exclusive creation and moves use the protocol's no-replace rename. FTP and FTPS cannot make the existence check and subsequent write or rename one atomic operation. Therefore their default `destinationPolicy: requireAtomicNoReplace` permits listing, reading, downloading, and deleting, but rejects `write` and `move`.

If a legacy FTP server is required and the application accepts this limitation, the connection must explicitly set `destinationPolicy: allowNonAtomic` and the worker policy must separately set `allowNonAtomicFtpDestinations: true`. Both are required. The connector still refuses destinations it observes already existing, but a concurrent writer can create a destination after that check and have its data overwritten. Keep the default when that behavior is unacceptable.

Cloud access verifies item ancestry against `rootFolderId`, bounded to 32 levels. SharePoint uses an explicit Graph drive ID for the selected document library; OneDrive uses its drive ID. Remote-item links and Google shortcuts are excluded. Native Google Workspace documents need an export-format operation, which this package does not currently expose; stored binary files are supported.

## Worker policy and deployment

The worker policy adds an independent destination allowlist to each connection's scope:

```json
{
  "origins": [
    "sftp://files.example.com:22",
    "https://graph.microsoft.com",
    "https://www.googleapis.com"
  ],
  "downloadOrigins": ["https://tenant.sharepoint.com"],
  "privateNetworks": [],
  "allowCleartextFtp": false,
  "allowNonAtomicFtpDestinations": false
}
```

Use exact origins, without wildcard hosts or paths. For Graph, configure the actual HTTPS origin used by the tenant's signed download URLs. Graph returns a preauthenticated redirect; the worker follows one allowed redirect with a separate request that carries no bearer credential. Arbitrary redirects and unexpected Google media redirects fail closed. HTTP egress resolves and pins approved addresses. FTP uses EPSV data connections to the pinned server; private servers require an explicit `privateNetworks` CIDR. Link-local metadata addresses remain prohibited.

| Environment variable | Meaning |
| --- | --- |
| `WEAVE_API_URL` | Platform API origin; HTTPS outside localhost |
| `WEAVE_ENVIRONMENT_URL` | Scoped `/api/v1/tenants/.../projects/.../environments/...` path |
| `WEAVE_WORKER_RELEASE_ID` | Admitted immutable worker release UUID |
| `WEAVE_WORKER_TOKEN_FILE` | Mounted rotating worker-token file |
| `WEAVE_FILES_POLICY_FILE` | Mounted policy JSON path |

```sh
uv run --project workers/files weave-files-worker
```

Build the container from the repository root. Use the checked-in dependency lock so the build uses the same package versions as the release:

```sh
# The Dockerfile installs the locked worker environment.
docker build -f workers/files/Dockerfile -t weave-files-worker:local .
```

The image runs as UID 65532. Mount credentials and policy read-only, allow a bounded writable temporary directory, and restrict network egress at deployment as well as in policy. The worker handles one task at a time and drains on SIGTERM. Transfers stop on cancellation or loss of task authority; every Weave upload/download request rechecks the task proof.

## Workflow example

```yaml
apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: receive-invoice, version: 1.0.0}
spec:
  inputSchema: {type: object}
  outputSchema: {}
  connections:
    incoming: {connector: weave-sftp@1.0.0, required: true}
  steps:
    - id: invoice
      kind: action
      uses: weave-sftp-download@1.0.0
      connection: incoming
      with:
        literal: {path: invoices/current.pdf}
  output: {ref: /steps/invoice/output}
```

Bind `incoming` to an authorized connection revision and the action to the admitted worker release during activation. The resulting reference can feed another action or a human-task attachment without embedding file bytes in the workflow.

## Complete your first transfer

First [connect the CLI and select a workspace](connect-to-api.md). If you operate a new installation, complete the [manual platform setup](standalone.md) first. Use an administrator for identity and grants, an authorized author/deployer for publication and activation, and a separate worker identity for execution. The [worker deployment walkthrough](../operations/deployment.md#provision-authority-and-private-worker-configuration) explains identity linking and the real IDs returned by admission; its example Action and token acquisition are specific to that tutorial, so use this package's exports and rotating token file here.

Follow these steps in order, retaining each successful response before continuing:

1. **Install and build.** Run the locked install, catalog export, release export, and Docker build above. Record the actual immutable image digest of the image you will deploy; a mutable tag such as `:local` is not a release identity. Establish the SFTP account's server-side root isolation, verify its host key, and prepare the worker policy before starting a transfer.

2. **Admit the release.** Create `files-release-request.json` by adding `image_digest` to the exported `/tmp/files-release-capabilities.json`, retaining both `capabilities` and `credential_capabilities`. Run `weave workers releases create --request files-release-request.json --output json` and save its returned release `id`. The [release contract](../reference/worker-protocol.md#authentication-and-endpoints) describes admission. These Actions execute in a remote worker; this package does not require native `connector_bindings`.

3. **Publish the contracts.** The catalog export is a compilation lock, not one publication request. For each selected `definitions[].document`, wrap its JSON text as `{"format":"json","source":"…"}` using the [publication-request walkthrough](cli-tutorial.md#3-validate-locally-then-publish). Publish the `weave-sftp` Connector first with `weave definitions publish --collection connectors --request sftp-connector-request.json --idempotency-key files-sftp-connector-1 --output json`; save its version `id`. Publish the `weave-sftp-download` Action the same way with `--collection actions` and its own request/key. Keep their exported definitions unchanged. Publish additional Connectors and Actions when you need them.

4. **Create the scoped connection.** Use `weave connections create --request sftp-connection-request.json --output json`. The request needs `name`, the published `connector_version_id`, the SFTP `config` shown above, `secretRef: {"password":"YOUR_SECRET_HANDLE"}`, and `allowed_destinations: ["sftp://files.example.com:22"]` with your actual approved origin. Save the returned **connection revision** `id`. Follow [Give integrations their secrets](../operations/identity-and-secrets.md#give-integrations-their-secrets) to provision and grant the handle; never put the password in the request. Restrict this example's `operations` to `download` if listing and metadata reads are unnecessary.

5. **Grant execution and credential access.** Follow [worker identity and grants](../operations/deployment.md#provision-authority-and-private-worker-configuration), using `weave remote access grant --request worker-grant.json` for the linked worker principal. The stock process registers all thirty exported task references, so its environment-scoped `worker` grant must cover the admitted release ID and all thirty references. Provider access remains restricted by connection operations, worker policy, secret-handle grants, and the exact credential grant. For this example, run `weave workers grant --request sftp-credential-grant.json` with `release_id`, `connection_revision_id`, and `capability: "weave-sftp.download@1.0.0"`. Grant only the connection/capability combinations you intend to execute.

6. **Publish and activate the workflow.** Save the YAML above as `receive-invoice.yaml`. Use [CLI publication and activation](cli-tutorial.md#3-validate-locally-then-publish), retaining the workflow version `id` and `digest`. Add these bindings to the activation request alongside `version_id`, `artifact_digest`, and your environment `scope`:

    ```json
    {
      "connection_revision_ids": {"incoming": "CONNECTION_REVISION_UUID"},
      "worker_release_ids": {"weave-sftp.download": "WORKER_RELEASE_UUID"}
    }
    ```

    Replace both placeholders with the saved response IDs. The worker map key is the task type without `@1.0.0`; do not substitute the Action name or use the native `connector_release_ids` map. Run `weave definitions activations create --request files-activation-request.json --idempotency-key receive-invoice-activate-1 --output json` and save the activation `id`.

7. **Start the worker, run, and download.** Set the five worker environment variables listed above, mount the rotating token and policy files, then start `weave-files-worker` using the locked command above or the admitted image. Follow [Start and inspect a run](cli-tutorial.md#5-start-and-inspect-a-run) with the saved activation ID and `input: {}`. Read the same run until `state.status` is `succeeded`; its `state.output` is the file reference. With a separately authorized `file_reader` identity, run `weave files download FILE_ID --to ./invoice-copy.pdf`, replacing `FILE_ID` with `state.output.id`. The [file CLI walkthrough](files.md#2-upload-your-first-file-with-the-cli) explains checksum verification and the refusal to overwrite an existing local file.

Publishing and activating do not start worker capacity. If the run waits, check the registered worker, release pin, and grants before starting another run. Keep publication, activation, and run idempotency keys when retrying the same request; inspect uncertain external effects before retrying an operation.

## Bounds and acceptance

Each file is limited to 25 MiB, with 256 KiB Weave chunks. A download is bounded and spooled before upload; uploads to providers use a verified Weave download. Size and digest mismatches fail before publishing a ready reference or beginning the provider write. Cloud JSON responses are limited to 1 MiB and 128 requests per task. Local protocol listings enumerate at most 1,000 entries and return at most 100 per page; pagination is a view of the live directory, not a snapshot transaction.

Inbound files can be retrieved with list/download actions invoked by an existing scheduled workflow. Dedicated provider change-feed triggers and atomically persisted polling cursors are **not implemented** in this package. Do not use a list cursor as a durable change-feed checkpoint.

Tests exercise real local FTP, SFTP, and FTPS servers, including certificate rejection, and mocked Graph/Google request contracts, scope restrictions and redirect credential isolation. They do not assert live Microsoft or Google access.

Provider references: [Graph download redirects](https://learn.microsoft.com/en-us/graph/api/driveitem-get-content?view=graph-rest-1.0), [Graph upload](https://learn.microsoft.com/en-us/graph/api/driveitem-put-content?view=graph-rest-1.0), [Google uploads](https://developers.google.com/workspace/drive/api/guides/manage-uploads), [Google downloads](https://developers.google.com/workspace/drive/api/guides/manage-downloads), [aioftp TLS](https://aioftp.aio-libs.org/client_api.html), [AsyncSSH host-key verification](https://asyncssh.readthedocs.io/en/latest/api.html).
