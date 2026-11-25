/*
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
*/
// One platform with transactions for every Operate page that shows them: the
// Runs list and its filter, a run's detail with its transactions, the
// incidents and their decisions, and the environment summary, which counts
// what the other pages list. A decision moves the platform on, so the pages
// agree with each other at every step.
import type { Page } from "@playwright/test";
import type {
  IncidentRecord,
  IncidentView,
} from "../../src/app/operate/incidents/incidents-model";
import type { OperationsSummary } from "../../src/app/operate/overview/overview-model";
import type {
  RunStatus,
  RunSummary,
} from "../../src/app/operate/run-contracts";
import type { RunTransactionView } from "../../src/app/operate/transactions/transaction-contracts";
import {
  runList,
  transactions,
  workflows,
} from "../operate-transactions-fixture";
import { NOW, highPriority, others } from "../transaction-incidents-fixture";
import {
  decisions,
  environment,
  incidentPlatform,
  type IncidentPlatform,
  type SentDecision,
} from "./incidents-po";

/** What a person who runs workflows may do, without a reconciler's decisions. */
export const operator = [
  "status.read",
  "run.read",
  "run.cancel",
  "catalog.read",
  "incident.read",
  "incident.resolve",
];
export const reconciler = [...operator, "transaction.reconcile"];

export interface TransactionsPlatform {
  incidents: IncidentPlatform;
  runs: RunSummary[];
  transactions: Record<string, RunTransactionView[]>;
  /** Every decision sent, in order. */
  sent: SentDecision[];
  /** What each list read of runs asked for, in order. */
  runQueries: URLSearchParams[];
  /** What a person may do: another person's from their next visit on. */
  as(capabilities: readonly string[]): Promise<void>;
  /** Changes a run as the list reports it. */
  run(id: string, facts: Partial<RunSummary>): void;
  /** Changes a run's only transaction. */
  transaction(run: string, facts: Partial<RunTransactionView>): void;
}

/** The environment summary of this moment: what the other pages list, counted. */
function summaryOf(platform: TransactionsPlatform): OperationsSummary {
  const states = Object.values(platform.transactions).flat();
  const undoing = states.filter((view) => view.state === "compensating");
  const active = platform.incidents.items.filter(
    (item): item is IncidentView =>
      !("unavailable" in item) && item.status === "active",
  );
  // In-doubt incidents come first among the incidents, as the platform sends them.
  const listed = [...active].sort(
    (a, b) =>
      Number(b.priority === "high") - Number(a.priority === "high") ||
      (b.opened_at ?? "").localeCompare(a.opened_at ?? ""),
  );
  const oldest = undoing
    .map((view) => Date.parse(view.opened_at))
    .sort((a, b) => a - b)[0];
  return {
    observed_at: NOW,
    api: {
      version: "0.1.0a20",
      build: null,
      instance: "0f8fad5b-d9cb-469f-a165-70867728950e",
      uptime_seconds: 86_400,
      compatibility: { mode: "ready", findings: 0 },
    },
    health: [
      { component: "database", state: "ok", latency_ms: 3.1 },
      { component: "recovery_loop", state: "ok", last_ok_at: NOW },
      { component: "schedules", state: "ok", last_ok_at: NOW },
      { component: "outbox_loop", state: "ok", last_ok_at: NOW },
      { component: "operations_loop", state: "ok", last_ok_at: NOW },
      { component: "alert_evaluator", state: "ok", last_ok_at: NOW },
      { component: "native_executors", state: "ok" },
      { component: "telemetry", state: "disabled" },
    ],
    load: {
      runs_active: platform.runs.filter((run) => !run.ended_at).length,
      tasks_ready: 0,
      tasks_leased: 2,
      oldest_ready_seconds: null,
      waits_active: 1,
      waits_overdue: 0,
      oldest_overdue_seconds: null,
      outbox_pending: 0,
      outbox_oldest_seconds: null,
      provider_pending: 0,
      sources_enabled: 1,
    },
    workers: {
      online: 2,
      offline: 0,
      draining: 0,
      capacity: 8,
      active_leases: 2,
    },
    targets: {
      total: 1,
      runner_online: 1,
      busy: 0,
      reconciliation_required: 0,
      awaiting_approval: 0,
    },
    incidents: { active: active.length, opened_24h: active.length },
    attention: listed.slice(0, 20).map((item) => ({
      kind: "incident" as const,
      id: item.id,
      title:
        item.priority === "high"
          ? `Transaction in doubt: ${item.code} at ${item.node_id}`
          : `Active incident in ${item.workflow?.name}`,
    })),
    transactions: {
      in_doubt: states.filter((view) => view.state === "in_doubt").length,
      undoing: undoing.length,
      ...(oldest === undefined
        ? {}
        : { oldest_undoing_seconds: (Date.parse(NOW) - oldest) / 1000 }),
    },
  };
}

/**
 * A platform at 10:54 with four transactions in doubt. `decided` moves it on
 * once a decision is recorded, before the decision's answer leaves.
 */
export async function transactionsPlatform(
  page: Page,
  options: {
    capabilities?: readonly string[];
    items?: IncidentRecord[];
    decided?: (platform: TransactionsPlatform, sent: SentDecision) => void;
  } = {},
): Promise<TransactionsPlatform> {
  const incidents = await incidentPlatform(page, {
    capabilities: [...(options.capabilities ?? operator)],
    items: options.items ?? [...highPriority, ...others],
  });
  const platform: TransactionsPlatform = {
    incidents,
    runs: structuredClone(runList),
    transactions: structuredClone(transactions),
    sent: [],
    runQueries: [],
    as: async (capabilities) => {
      await page.route("**/studio/api/api/v1/identity", (route) =>
        route.fulfill({
          json: {
            principal_id: "human",
            kind: "human",
            grants: [
              {
                role: "test",
                scope: {
                  tenant_id: "tenant",
                  project_id: "project",
                  environment_id: "development",
                },
                resources: [],
                capabilities,
              },
            ],
            workspaces: [],
            truncated: false,
          },
        }),
      );
      await page.reload();
    },
    run: (id, facts) => {
      platform.runs = platform.runs.map((run) =>
        run.id === id ? { ...run, ...facts } : run,
      );
    },
    transaction: (run, facts) => {
      const [view, ...rest] = platform.transactions[run];
      platform.transactions[run] = [{ ...view, ...facts }, ...rest];
    },
  };
  await page.route("**/run-summaries?**", (route) => {
    const query = new URL(route.request().url()).searchParams;
    platform.runQueries.push(query);
    const statuses = query.getAll("status");
    const states = query.getAll("transaction_state");
    return route.fulfill({
      json: {
        items: platform.runs.filter(
          (run) =>
            (!statuses.length || statuses.includes(run.status)) &&
            (!states.length ||
              (!!run.transaction_state &&
                states.includes(run.transaction_state))),
        ),
        next_cursor: null,
      },
    });
  });
  await page.route(`${environment}/runs/*`, (route) => {
    const id = new URL(route.request().url()).pathname.split("/").pop()!;
    const run = platform.runs.find((item) => item.id === id);
    if (!run)
      return route.fulfill({
        status: 404,
        json: { code: "WV-NOT-FOUND", message: "Run not found" },
      });
    // What holds the run, by the platform's own keys: "add#undo".
    const stops = platform.incidents.items.flatMap((item) =>
      !("unavailable" in item) && item.run_id === id && item.status === "active"
        ? [item]
        : [],
    );
    const keyOf = (item: IncidentView) => item.instance_key ?? item.node_id!;
    return route.fulfill({
      json: {
        id,
        business_key: run.business_key,
        correlation_key: run.correlation_key,
        activation: {
          id: run.activation_id,
          name: run.workflow.name,
          revision: run.activation.revision,
          request: { version_id: run.workflow.definition_version_id },
        },
        state: {
          status: run.status,
          active: stops.map(keyOf),
          input: {},
          ...(run.paused ? { manual_paused: true } : {}),
          // Of several incidents the platform names the first key's code.
          ...(stops.length
            ? {
                incident: [...stops].sort((a, b) =>
                  a.incident_key < b.incident_key ? -1 : 1,
                )[0].code,
              }
            : {}),
          incidents: Object.fromEntries(
            stops.map((item) => [
              item.incident_key,
              { node_id: keyOf(item), code: item.code },
            ]),
          ),
        },
      },
    });
  });
  await page.route("**/projects/project/workflows/*/export", (route) => {
    const id = new URL(route.request().url()).pathname.split("/").at(-2)!;
    return route.fulfill({ json: { id, document: workflows[id] } });
  });
  await page.route(`${environment}/runs/*/lifecycle`, (route) =>
    route.fulfill({ json: { archived: false, purged: false, revision: 1 } }),
  );
  await page.route(`${environment}/runs/*/steps?*`, (route) =>
    route.fulfill({ json: { items: [], next_cursor: null, complete: true } }),
  );
  await page.route(`${environment}/runs/*/transactions?*`, (route) => {
    const id = new URL(route.request().url()).pathname.split("/").at(-2)!;
    return route.fulfill({
      json: { items: platform.transactions[id] ?? [], next_cursor: null },
    });
  });
  await page.route(`${environment}/operations/summary`, (route) =>
    route.fulfill({ json: summaryOf(platform) }),
  );
  platform.sent = await decisions(page, incidents, (_route, sent) => {
    options.decided?.(platform, sent);
    return false;
  });
  return platform;
}

/** A run that is over, as the list reports it. */
export const ended = (
  run: RunSummary,
  status: RunStatus,
  state: RunSummary["transaction_state"],
): Partial<RunSummary> => ({
  status,
  ended_at: NOW,
  updated_at: NOW,
  duration_ms: Date.parse(NOW) - Date.parse(run.started_at),
  active_incidents: 0,
  transaction_state: state,
});
