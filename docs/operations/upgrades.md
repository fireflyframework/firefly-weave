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

# Upgrades and compatibility

Weave uses explicit forward migrations and checks persisted requirements before
admitting effect-producing work. Starting an API never migrates its database.
The current expected schema revision is `0021_operations`; both Alembic's revision
and Weave's schema sentinel must match the installed artifact.

## Understand the upgrade boundary

An upgrade has three independent checks: the new artifact can read the database
schema, its installed capabilities satisfy retained execution requirements, and
real work can run successfully after restart. A completed migration proves only
the first of these. Workflows may still reference an older admitted worker release
or connector version that the new deployment must continue to support.

Use a maintenance window for this procedure. The local walkthrough can rehearse
these steps, but its guarded restore helper is not a production backup system.
For production, use a verified environment-specific backup/restore procedure and
account for external effects that happened after the backup.

## Prepare the upgrade

1. Record the running wheel hash, dependency lock, image identities, schema
   revision, and private configuration locations. Keep the previous artifact.
2. Inventory every writer: APIs with schedulers, native executors, remote workers,
   provider dispatchers, broker consumers, and outbox dispatchers. Stop admission
   and fence those writers before database maintenance.
3. For the owned local installation, follow [backup and restore](backup-restore.md)
   to create and verify a recoverable copy. For other deployments, use the
   environment-specific verified recovery procedure. Preserve the source database
   and external effect receipts.
4. Install the selected exact artifact in a separate environment and rehearse the
   migration against a restored copy. Use the same connector packages and
   operational policy intended for the target deployment.
5. Provide explicit migration authority. Runtime application and scheduler
   credentials are separate nonowner identities; they cannot replace migration
   authority. The operations migration provisions narrowly privileged function
   ownership and rejects an unrecognized or privileged existing owner role.

Do not run mixed schema versions against the same mutable database during this
procedure. The server performs exact schema checks, not rolling schema negotiation.

## Apply packaged migrations

Run this stage in the operator terminal while writers are stopped. Select the
new installed interpreter, not whichever `python` or source checkout happens to
be on `PATH`. On a restored local rehearsal, the target URLs are in that attempt's
`target.env`; the original `runtime.env` still refers to the fenced source.
Confirm which database you selected before invoking the migration.

Load the migration URL from protected deployment configuration. Do not print it or
pass it to runtime containers. Invoke the selected installed environment:

```sh
"$WEAVE_PYTHON" -I -m firefly_weave.persistence.migrations
```

`WEAVE_PYTHON` is the Python executable in that installed environment.
`WEAVE_MIGRATION_DATABASE_URL` must be present for this command. If you lower
operational quotas at initial migration, supply the same
`WEAVE_OPERATIONS_POLICY` JSON to migration and runtime processes. The command
prints `Weave schema is current` only after checking both schema markers.

Migrations use a transaction and serialize explicit migration invocations.
Failure must be investigated against the retained database and protected logs;
do not change version rows to bypass a failed migration. Destructive downgrades
are unsupported. Returning to an older runtime requires a compatible separately
restored database and reconciliation of any external effects since its backup.

Operational policy has a persisted fingerprint. Every replica must use the same
policy as the database. Rerunning an already-current migration is not a policy
update command. This release does not expose an in-place policy-change operation;
a configuration mismatch leaves the runtime restricted. Do not manually rewrite
the fingerprint or counters to force readiness.

## Start and inspect compatibility

Remove migration credentials from the runtime environment and start the selected
artifact with its application and execute-only catalog/scheduler identities.
Startup inventory still requires catalog authority when
`WEAVE_SCHEDULER_ENABLED=false`.
The two URLs must name the same driver, host, port, database, and query options;
only their credentials differ. Use the same host spelling for both URLs, even
when multiple DNS aliases resolve to the same server. The catalog login must
retain its execute-only scheduler authority, without business-table or column
grants.

Compatibility checks examine retained runs, activations, worker releases,
connector requirements, provider requirements, and operational policy. Exact
persisted versions and pins matter; an online worker is not a substitute for a
compatible admitted release. Missing authority or an incomplete inventory does
not count as a successful check.

Reading the report requires project-scoped `status.read` authority. Requesting a
new scan additionally requires `compatibility.check`:

```sh
env -u WEAVE_ENVIRONMENT_ID "$WEAVE_PYTHON" -I -m firefly_weave.cli.main \
  compatibility read --output json
env -u WEAVE_ENVIRONMENT_ID "$WEAVE_PYTHON" -I -m firefly_weave.cli.main \
  compatibility check --output json
```

Before these commands, configure `WEAVE_BASE_URL` to the restarted API origin,
`WEAVE_TENANT_ID` and `WEAVE_PROJECT_ID` to the project being checked, and a current
`WEAVE_ACCESS_TOKEN` or the [CLI login configuration](../reference/cli.md#login-and-secure-persistence).
For a local rehearsal, obtain scope IDs from `first-run.json`; do not invent new
IDs or reuse an expired token receipt. The `env -u WEAVE_ENVIRONMENT_ID` prefix
removes environment scope because compatibility is a project-level operation. `read`
returns the current report; `check` requests a fresh scan. Findings are filtered
to the authorized project, with safe global inventory conditions retained. The
report includes `mode`, `complete`, `checked_at`, and `findings_truncated`; a
truncated or incomplete report is not evidence of full compatibility. No foreign
project's resource identifiers should appear in your scoped report.

The process also requests a compatibility rescan every 60 seconds. Automatic and
explicit scans share one reservation; overlapping automatic scans are skipped
instead of queued. A failed scan leaves the process restricted, and the next
periodic scan retries. Catalog cleanup must succeed before readiness is restored.

The capabilities response also reports effective policy, fixed server ceilings,
the supported worker convention, and a small readiness projection. An older
server may omit this declaration; omission does not imply readiness.

| Finding | Operator action |
| --- | --- |
| `authority_missing`, `inventory_incomplete` | Repair catalog connectivity/authority and rerun the scan |
| `policy_mismatch` | Restore the policy matching the database; do not edit counters or fingerprints |
| `ir_unsupported`, `artifact_invalid` | Inspect the pinned definition and select a compatible artifact; preserve historical bytes |
| `action_unavailable`, `connector_unsupported`, `provider_requirement_unsupported` | Restore the exact required release/package or use an explicit supported migration |
| `worker_protocol_unsupported` | Use a compatible worker/server convention; do not relabel an existing release |
| `legacy_policy_blocked` | Inspect historical classification limits; preserve withheld evidence rather than bypassing policy |
| `operational_capacity_blocked`, `capacity_absent` | Inspect retained usage and admission requirements; use authorized maintenance where supported |

Restricted mode keeps health and authorized inspection available while blocking
new effect-producing work. Issued terminal controls remain available within their
reserved authority and capacity. It does not permit arbitrary execution or
rewriting retained definitions to silence findings. See
[incident operations](../reference/incident-operations.md) and
[retention](retention.md) for supported controls.

## Verify before admitting traffic

Perform these checks in order for each project and intended execution path:

1. Check `/health/ready` for the replica receiving traffic. If it fails, leave
   effect-producing admission stopped and inspect the compatibility findings.
2. Read a complete compatibility report with `mode=ready`, and confirm its policy
   matches the intended deployment. A report truncated for response size cannot
   establish that all requirements were inspected by your review.
3. Read a known historical run and its durable output to check retained access.
4. Start the required admitted executor/worker and run a small authorized workflow.
   The [deployment exercises](deployment.md) show concrete remote and native checks
   for the local installation.
5. Verify external receiver/provider receipts and telemetry through their own
   interfaces, then deliberately reenable the remaining writers.

Confirm `/health/ready` succeeds, compatibility is complete and ready, and the
reported policy matches the intended deployment. Run an authorized workflow
through the installed API and an admitted worker, verify its durable outcome, and
inspect telemetry independently. Reenable writers deliberately and monitor
recovery; do not infer provider delivery from API readiness alone.

Keep the prior artifact, source database, backup, and protected migration evidence
until the upgraded deployment and its recovery procedure have been verified.

## If the upgrade fails

Keep the failed deployment's evidence and stop its writers before selecting a
recovery path. A schema failure belongs to migration diagnosis; a compatibility
finding belongs to the missing authority, policy, or pinned capability listed in
the report. Neither is repaired by editing version rows or weakening grants.

Starting the old wheel against a newly migrated database is not a supported
rollback. Recover the old artifact with a separately restored compatible database,
choose one active deployment, and reconcile effects that occurred after that
backup before resuming work. See [incident operations](../reference/incident-operations.md)
for ambiguous effects and [backup/restore](backup-restore.md) for the local source
fencing behavior.
