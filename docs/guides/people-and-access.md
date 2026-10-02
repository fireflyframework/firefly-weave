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

# Give people the right access

Weave separates **sign-in** from **permission to do work**. Your configured CIAM
provider authenticates a person. A Weave principal links that identity to local
role bindings. Each binding names a role and the tenant, project, environment,
and optional resources where it applies.

The administration API, CLI, SDK, and Studio controls described here are included
in alpha6. Use a matching server/client and the [Studio installation](studio.md).
Alpha4 predates these administration contracts.

## 1. Choose who manages which part

| Responsibility | Where it belongs |
| --- | --- |
| Create the sign-in account, reset a password, configure MFA | Your CIAM administration, such as Entra ID or Keycloak |
| Create a Weave principal and link its verified provider identity | Weave platform administrator |
| Grant or remove access in a tenant | Weave tenant administrator for that tenant, or platform administrator |
| Define workflows | A principal with `developer` grants in the relevant project |
| Operate executions | A principal with the required run permissions in the relevant scope |
| Claim and complete assigned approvals | An eligible principal with `task_participant` permission |

Studio does not collect CIAM passwords, create provider accounts, or copy external
role names into Weave grants. Keycloak is used by the local development setup;
your configured provider can be different. Follow
[identity-provider setup](../operations/identity-and-secrets.md) for trust profiles.

The first platform administrator must be established through the existing
privileged bootstrap procedure. It is not a self-registration button in Studio.

## 2. Choose the smallest useful role combination

Roles are **additive**, not a hierarchy. An administrator is not automatically a
workflow author or run operator. This avoids giving every administrator access
to business payloads merely because they can manage memberships.

| User-facing responsibility | Weave role | Main permission |
| --- | --- | --- |
| Workflow editor | `developer` | Author/publish definitions, compile, simulate, read the catalog |
| Viewer | `viewer` | Read catalog, run state, status, and delivery information |
| Run operator | `operator` | Start/signal/cancel/pause/resume/archive runs and handle incidents |
| Deployer | `deployer` | Activate releases and configure activation/trigger bindings |
| Tenant administrator | `tenant_admin` | Manage tenant projects, environments, grants, and connections |
| Platform administrator | `platform_admin` | Manage platform identities, top-level tenants, and grants |
| Human reviewer | `task_participant` | Read eligible tasks, claim/release, and complete assigned work |
| Human-task manager | `task_manager` | Manage assignments and human tasks |
| Execution content manager | `execution_manager` | Read, archive, and explicitly purge eligible execution content |
| Email reader / sender / manager | `email_reader`, `email_sender`, `email_manager` | Read conversations, send messages, or manage sources according to the selected role |

For someone who inspects and operates runs, grant **both `viewer` and `operator`**
in the required scope. For an author who also activates workflows, add `deployer`
deliberately. Approval and email permissions remain separate. A worker identity
is constrained to worker capabilities even if an invalid human role is attached.

A tenant-level binding covers descendants. A project binding covers that project
and its environments. An environment binding is narrower. Resource restrictions
further constrain supported operations; they do not turn an environment grant
into tenant administration authority.

## 3. Create and link a person

As a platform administrator:

1. Confirm the person already has a sign-in account with the configured provider.
2. Obtain the exact **provider ID**, **issuer**, and **subject** from trusted
   provider administration or a verified identity. The subject is not normally
   an email address or display name.
3. Create a Weave principal of kind `human` and retain its returned UUID.
4. Link the provider/issuer/subject tuple to that principal.
5. Add the required scoped role bindings in the intended tenant.
6. Have the person sign in with their own CLI/Studio profile and confirm the
   authorized workspace and operations are visible.

A principal can exist before its identity link or grants, but cannot perform the
intended work until all required pieces exist. Linking an identity already owned
by another principal fails; the operation does not transfer the account.

### Perform these steps in Studio

1. Complete the [connection assistant](studio.md#use-the-connection-assistant)
   with your administrator identity, then select the intended workspace.
2. Open **Settings → People and access** and select **Refresh access directory**.
   The principal directory appears only for platform administrators. Tenant
   administrators see role bindings for their selected tenant.
3. In **Principal directory**, choose **Human** and select **Create principal**.
   Select the resulting directory row to copy its UUID into the forms below.
4. Under **Link an existing CIAM identity**, enter the verified provider ID,
   exact issuer URL, and exact CIAM subject. Select **Link identity**.
5. Under **Tenant role bindings**, choose a role and grant scope. Start with
   **Selected environment** unless the person needs broader access. Select
   **Grant role** and inspect the new row. Add another binding when the person
   needs an additional role.
6. Ask the person to sign in with their own account and test the connection.
   Your administrator session cannot demonstrate what their account may do.

If the directory is absent, check your current identity and permissions instead
of creating a second account. If creation has an uncertain network result,
Studio stops another creation attempt: refresh and reconcile the returned
directory before selecting **I reconciled the directory**. Deactivation and
binding revocation show a confirmation naming the affected principal or binding.
Switching workspaces clears the previous directory and unfinished access forms.

### Perform the same steps with the CLI

First complete [Connect to an API](connect-to-api.md). Keep its API and tenant
variables and approved authentication options in this terminal. Use the
source-built `.venv/bin/weave` when following examples from a checkout.

```bash
# Create a request for a human principal; no password or token goes in this file.
printf '%s\n' '{"kind":"human"}' > person-request.json

# Requires platform administrator authority, not just a tenant administrator role.
weave remote access principals create "${WEAVE_AUTH_OPTIONS[@]}" \
  --request person-request.json
```

Record the returned `id` as `WEAVE_PERSON_ID`. Do not repeat creation after an
uncertain network result without checking the directory: creation allocates a
new principal each time.

```bash
# Read the returned UUID instead of inventing one.
printf 'Created principal UUID: '; read -r WEAVE_PERSON_ID
export WEAVE_PERSON_ID
```

Create `identity-link.json` using the **real verified values** supplied by your
identity administrator. This illustrative shape is not an executable provider
configuration:

```json
{
  "provider_id": "your-configured-provider-id",
  "issuer": "https://your-provider.example/your-tenant",
  "subject": "verified-provider-subject"
}
```

```bash
# Link this existing provider identity to the new Weave principal.
weave remote access principals link "$WEAVE_PERSON_ID" \
  "${WEAVE_AUTH_OPTIONS[@]}" --request identity-link.json
```

For example, generate a viewer grant for the project selected in your existing
client environment:

```bash
# Use real IDs retained from the API connection guide.
python3 - <<'PYTHON'
import json
import os
from pathlib import Path
request = {
    "principal_id": os.environ["WEAVE_PERSON_ID"],
    "role": "viewer",
    "project_id": os.environ["WEAVE_PROJECT_ID"],
    "resources": [],
}
Path("viewer-grant.json").write_text(json.dumps(request, indent=2) + "\n")
PYTHON

# Requires tenant-level grant management for the selected tenant.
weave remote access members grant "${WEAVE_AUTH_OPTIONS[@]}" \
  --request viewer-grant.json
```

The member endpoint takes the tenant from the URL/client scope. The body can
narrow it with a project and environment; it cannot select another tenant or
create a platform-administrator binding.

## 4. Review, revoke, or deactivate

```bash
# Tenant administrators see the bindings in the selected tenant.
weave remote access members list "${WEAVE_AUTH_OPTIONS[@]}" --limit 50

# Platform administrators can inspect the global principal directory.
weave remote access principals list "${WEAVE_AUTH_OPTIONS[@]}" --limit 50
```

List responses contain `items` and `next_cursor`. Pass a returned cursor with
`--cursor` to continue the same collection and scope. A membership row's `id` is
the **binding ID**, while `principal_id` identifies the person. Revocation takes
the binding ID, so removing one role does not remove every role the person has.

```bash
# Revoke only the binding you reviewed; use its returned UUID.
printf 'Binding UUID to revoke: '; read -r WEAVE_BINDING_ID
weave remote access members revoke "$WEAVE_BINDING_ID" "${WEAVE_AUTH_OPTIONS[@]}"
```

Global principal deactivation is a platform-administrator operation. It prevents
subsequent use of that Weave identity; it does not delete historical decisions or
disable the CIAM account itself. The UI/API rejects deactivating the current
administrator and removing that caller's own tenant-administrator binding.

Grants are checked again by the server. A button visible in an already open
browser does not preserve permission after revocation. In-flight operations may
already have passed their authorization check; revocation cannot undo external
effects that have already happened.

## 5. Use the same API from your product

The [HTTP API explorer](../reference/api-explorer.md) exposes request and response
schemas for `principals.*` and `members.*`. Swagger on your running platform can
execute these requests using your own authorized token.

For Python, wrap an already configured `WeaveClient` with `MembersClient`:

```python
from firefly_weave.sdk.members import MembersClient

async def read_members(weave_client):
    # The client supplies the existing login and selected tenant scope.
    access = MembersClient(weave_client)
    page = await access.bindings(limit=50)
    return page.items, page.next_cursor
```

The helper also provides `create_principal`, `link_identity`, `set_active`,
`grant`, and `revoke`. They use the same contracts and server checks as Studio
and the CLI. Account provisioning in a third-party CIAM remains a separate
provider integration.
