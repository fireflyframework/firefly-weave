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
// One moment in one environment, 10:54 on 18 November, as the platform's
// alert operations answer it: two alerts firing (one critical), three
// resolved, eight rules in every state a rule can show, two email channels,
// the settings, one opt-in and the retention periods. Wire JSON only, so the
// Python contract can check every record (alerts-oracle.ts), and the unit
// tests and browser specs share it.

export const NOW = "2026-11-18T10:54:10Z";
/** The signed-in person, and the other members the channels name. */
export const ME = "c4a1e0d2-5b7f-4e39-9a61-0f2d3c4b5a69";
export const BEN = "b3e5f7a9-1c2d-4e6f-8a0b-1c2d3e4f5a6b";
export const ANA = "a2d4f6b8-0c1e-4f3a-9b5c-7d9e1f3a5c7e";
export const ids = {
  oncall: "c1000000-0000-4000-8000-000000000001",
  team: "c1000000-0000-4000-8000-000000000002",
  acmeMail: "e1000000-0000-4000-8000-000000000001",
  opsMail: "e1000000-0000-4000-8000-000000000002",
  k3d: "f1000000-0000-4000-8000-000000000001",
  kind: "f1000000-0000-4000-8000-000000000002",
  failureRate: "d1000000-0000-4000-8000-000000000002",
  runnerOffline: "d1000000-0000-4000-8000-000000000004",
  degraded: "d1000000-0000-4000-8000-000000000001",
  noWorkers: "d1000000-0000-4000-8000-000000000003",
  tasksWaiting: "d1000000-0000-4000-8000-000000000005",
  slowRuns: "d1000000-0000-4000-8000-000000000006",
  reconcile: "d1000000-0000-4000-8000-000000000007",
  inDoubt: "d1000000-0000-4000-8000-000000000008",
  firingRate: "a1000000-0000-4000-8000-000000000001",
  firingRunner: "a1000000-0000-4000-8000-000000000002",
  consent: "e2000000-0000-4000-8000-000000000001",
} as const;

const created = "2026-11-02T09:00:00Z";
const rule = (
  id: string,
  facts: Record<string, unknown>,
  state: Record<string, unknown>,
) => ({
  id,
  revision: 1,
  name: "",
  enabled: true,
  selector: {},
  for_seconds: 0,
  min_samples: 1,
  channel_ids: [],
  renotify_seconds: 0,
  created_at: created,
  updated_at: created,
  updated_by: BEN,
  ...facts,
  state: {
    since: "2026-11-18T10:00:00Z",
    evaluated_at: "2026-11-18T10:54:00Z",
    no_data: false,
    ...state,
  },
});

/** The Rules tab, in the order the platform lists them. */
export const rules = () => [
  rule(
    ids.degraded,
    {
      name: "A platform component is degraded",
      signal: "health.degraded",
      comparator: "gt",
      threshold: 0,
      window_seconds: null,
      for_seconds: 300,
      severity: "warning",
      preset_id: "platform-degraded",
      updated_by: undefined,
    },
    { state: "ok", value: 0, unit: "count" },
  ),
  rule(
    ids.failureRate,
    {
      name: "Run failure rate above 10 %",
      signal: "run.failure_rate",
      comparator: "gt",
      threshold: 0.1,
      window_seconds: 900,
      min_samples: 10,
      severity: "critical",
      channel_ids: [ids.oncall, ids.team],
      renotify_seconds: 3600,
    },
    {
      state: "firing",
      since: "2026-11-18T10:42:00Z",
      value: 0.146,
      unit: "ratio",
      alert_id: ids.firingRate,
    },
  ),
  rule(
    ids.noWorkers,
    {
      name: "No workers online",
      signal: "workers.online",
      comparator: "lt",
      threshold: 1,
      window_seconds: null,
      for_seconds: 120,
      severity: "critical",
      channel_ids: [ids.oncall],
    },
    { state: "ok", value: 2, unit: "count" },
  ),
  rule(
    ids.runnerOffline,
    {
      name: "A runner is offline",
      signal: "targets.runner_offline",
      selector: { target_id: ids.k3d },
      comparator: "gt",
      threshold: 0,
      window_seconds: null,
      for_seconds: 120,
      severity: "warning",
      channel_ids: [ids.team],
    },
    {
      state: "firing",
      since: "2026-11-18T10:16:00Z",
      value: 1,
      unit: "count",
      alert_id: ids.firingRunner,
    },
  ),
  rule(
    ids.tasksWaiting,
    {
      name: "Tasks waiting over 5 minutes",
      signal: "queue.oldest_ready_seconds",
      comparator: "gt",
      threshold: 300,
      window_seconds: null,
      for_seconds: 120,
      severity: "warning",
      channel_ids: [ids.oncall],
    },
    { state: "ok", value: 42, unit: "seconds" },
  ),
  rule(
    ids.slowRuns,
    {
      name: "Slow invoice runs",
      signal: "run.duration_p95",
      selector: { workflow: "invoice-approval" },
      comparator: "gt",
      threshold: 30,
      window_seconds: 3600,
      severity: "info",
      channel_ids: [ids.team],
    },
    {
      state: "ok",
      unit: "seconds",
      evaluated_at: "2026-11-18T10:44:00Z",
      error: { code: "WV-UNAVAILABLE", at: "2026-11-18T10:52:00Z" },
    },
  ),
  rule(
    ids.reconcile,
    {
      name: "Reconciliation needed on the cluster",
      signal: "deployments.reconciliation_required",
      selector: { target_id: ids.kind },
      comparator: "gt",
      threshold: 0,
      window_seconds: null,
      severity: "warning",
      channel_ids: [ids.team],
    },
    { state: "unauthorized", unit: "count", evaluated_at: undefined },
  ),
  rule(
    ids.inDoubt,
    {
      name: "Transactions in doubt",
      signal: "transactions.in_doubt",
      comparator: "gt",
      threshold: 0,
      window_seconds: null,
      for_seconds: 300,
      severity: "critical",
      channel_ids: [ids.oncall],
    },
    { state: "unsupported", evaluated_at: undefined },
  ),
];

const notification = (
  channel: string,
  state: string,
  code: string | null = null,
  at = "2026-11-18T10:42:31Z",
) => ({
  channel_id: channel,
  kind: "firing",
  state,
  ...(code ? { code } : {}),
  at,
});
const alert = (id: string, facts: Record<string, unknown>) => ({
  id,
  rule_revision: 1,
  notifications: [],
  ...facts,
});

/** Firing alerts, newest first. */
export const firing = () => [
  alert(ids.firingRate, {
    rule_id: ids.failureRate,
    rule_name: "Run failure rate above 10 %",
    signal: "run.failure_rate",
    severity: "critical",
    state: "firing",
    started_at: "2026-11-18T10:42:00Z",
    comparator: "gt",
    threshold: 0.1,
    unit: "ratio",
    value_at_start: 0.138,
    last_value: 0.146,
    notifications: [
      notification(ids.oncall, "sent"),
      notification(ids.team, "failed", "mail_refused"),
    ],
  }),
  alert(ids.firingRunner, {
    rule_id: ids.runnerOffline,
    rule_name: "A runner is offline",
    signal: "targets.runner_offline",
    severity: "warning",
    state: "firing",
    started_at: "2026-11-18T10:16:00Z",
    comparator: "gt",
    threshold: 0,
    unit: "count",
    value_at_start: 1,
    last_value: 1,
    acknowledged_at: "2026-11-18T10:20:00Z",
    acknowledged_by: ME,
    notifications: [
      notification(
        ids.team,
        "skipped_limit",
        "skipped_limit",
        "2026-11-18T10:18:31Z",
      ),
    ],
  }),
];

/** Resolved alerts of the last 7 days, newest first. */
export const resolved = () => [
  alert("a1000000-0000-4000-8000-000000000003", {
    rule_id: ids.tasksWaiting,
    rule_name: "Tasks waiting over 5 minutes",
    signal: "queue.oldest_ready_seconds",
    severity: "warning",
    state: "resolved",
    started_at: "2026-11-18T08:14:00Z",
    resolved_at: "2026-11-18T08:23:00Z",
    resolved_reason: "threshold",
    comparator: "gt",
    threshold: 300,
    unit: "seconds",
    value_at_start: 340,
    last_value: 120,
  }),
  alert("a1000000-0000-4000-8000-000000000004", {
    rule_id: ids.degraded,
    rule_name: "A platform component is degraded",
    signal: "health.degraded",
    severity: "warning",
    state: "resolved",
    started_at: "2026-11-17T22:40:00Z",
    resolved_at: "2026-11-17T22:54:00Z",
    resolved_reason: "threshold",
    comparator: "gt",
    threshold: 0,
    unit: "count",
    value_at_start: 1,
    last_value: 0,
  }),
  alert("a1000000-0000-4000-8000-000000000005", {
    rule_id: "d1000000-0000-4000-8000-000000000009",
    rule_name: "Incidents are open",
    signal: "incidents.active",
    severity: "info",
    state: "resolved",
    started_at: "2026-11-17T16:02:00Z",
    resolved_at: "2026-11-17T18:12:00Z",
    resolved_reason: "rule_disabled",
    comparator: "gt",
    threshold: 0,
    unit: "count",
    value_at_start: 1,
  }),
];

/** An alerts.list answer. */
export const alertPage = (
  items: unknown[],
  firingCount = 2,
  critical = 1,
  next: string | null = null,
) => ({ items, next_cursor: next, firing: firingCount, critical });

/** The Channels tab: one the viewer owns, one Ben owns with Ana unreachable. */
export const channels = () => [
  {
    id: ids.oncall,
    revision: 2,
    kind: "email",
    name: "On-call email",
    connection_revision_id: ids.acmeMail,
    owner_id: ME,
    recipients: [{ member: BEN }, { member: ME }],
    recipient_states: [
      { index: 0, state: "ok" },
      { index: 1, state: "ok" },
    ],
    status: "ok",
  },
  {
    id: ids.team,
    revision: 4,
    kind: "email",
    name: "Platform team",
    connection_revision_id: ids.opsMail,
    owner_id: BEN,
    recipients: [
      { member: BEN },
      { member: ME },
      { member: ANA },
      { address: "oncall@vendor.example" },
    ],
    recipient_states: [
      { index: 0, state: "ok" },
      { index: 1, state: "ok" },
      { index: 2, state: "without_address" },
      { index: 3, state: "ok" },
    ],
    status: "ok",
  },
];

export const presets = () => [
  {
    id: "platform-degraded",
    version: 1,
    install: "default",
    name: "A platform component is degraded",
    rule: {
      name: "A platform component is degraded",
      enabled: true,
      signal: "health.degraded",
      selector: {},
      comparator: "gt",
      threshold: 0,
      window_seconds: null,
      for_seconds: 300,
      min_samples: 1,
      severity: "warning",
      channel_ids: [],
      renotify_seconds: 0,
    },
    installed_rule_id: ids.degraded,
  },
  {
    id: "run-failure-rate",
    version: 1,
    install: "suggested",
    name: "Run failure rate above 10 %",
    rule: {
      name: "Run failure rate above 10 %",
      enabled: true,
      signal: "run.failure_rate",
      selector: {},
      comparator: "gt",
      threshold: 0.1,
      window_seconds: 900,
      for_seconds: 0,
      min_samples: 10,
      severity: "warning",
      channel_ids: [],
      renotify_seconds: 0,
    },
  },
  {
    id: "no-workers",
    version: 1,
    install: "suggested",
    name: "No workers online",
    rule: {
      name: "No workers online",
      enabled: true,
      signal: "workers.online",
      selector: {},
      comparator: "lt",
      threshold: 1,
      window_seconds: null,
      for_seconds: 120,
      min_samples: 1,
      severity: "critical",
      channel_ids: [],
      renotify_seconds: 0,
    },
  },
];

export const settings = () => ({
  recipient_domains: ["vendor.example"],
  approval_channel_ids: [ids.team],
  revision: 3,
});

export const consents = () => [
  {
    id: ids.consent,
    domain: "vendor.example",
    address: "oncall@vendor.example",
    method: "administrator_attested",
    statement: "Agreed by email to the on-call rota, 3 Nov",
    created_by: BEN,
    created_at: "2026-11-04T09:12:00Z",
  },
];

/** Preview of the rule the editor draws: no data yet, one ended run. */
export const previewNoData = () => ({
  value: null,
  unit: "ratio",
  condition_met: false,
  samples: 1,
  evaluated_at: NOW,
  no_data_reason: "too_few_samples",
});
export const previewValue = () => ({
  value: 42,
  unit: "seconds",
  condition_met: false,
  evaluated_at: NOW,
});

export const myAddress = () => ({
  address: "carla@acme.example",
  updated_at: "2026-11-10T08:00:00Z",
});

export const retentionPeriods = () => ({
  items: [
    { record: "run_statistics_5m", seconds: 2 * 86_400 },
    { record: "run_statistics_1h", seconds: 35 * 86_400 },
    { record: "run_statistics_1d", seconds: 400 * 86_400 },
    { record: "samples_1m", seconds: 86_400 },
    { record: "samples_1h", seconds: 35 * 86_400 },
    { record: "change_requests", seconds: 90 * 86_400 },
    { record: "alerts", seconds: 30 * 86_400 },
    { record: "alert_events", seconds: 30 * 86_400 },
    { record: "execution_logs", seconds: 7 * 86_400 },
  ],
});

/** Email connections the viewer may send from, as bindable-connections lists them. */
export const mailConnections = () => ({
  items: [
    {
      connection_id: "e0000000-0000-4000-8000-000000000001",
      revision_id: ids.acmeMail,
      name: "acme-mail",
      connector: "weave-email@1.0.0",
      enabled: true,
    },
  ],
  next_cursor: null,
});

/** Deployment targets the rules name. */
export const targets = () => ({
  items: [
    { id: ids.k3d, name: "k3d-weave-test.team-a" },
    { id: ids.kind, name: "kind-weave-test" },
  ],
  next_cursor: null,
});

/** Tenant members as members.list answers them (one binding each). */
export const members = () => ({
  items: [BEN, ME, ANA].map((principal, index) => ({
    id: `b0000000-0000-4000-8000-00000000000${index + 1}`,
    principal_id: principal,
    kind: "human",
    active: true,
    role: "operator",
    scope: { tenant_id: "tenant", project_id: null, environment_id: null },
    resources: [],
  })),
  next_cursor: null,
});

/** Two signed webhook subscriptions receive alert changes; one doesn't. */
export const subscriptions = () => ({
  items: [
    {
      id: "5a000000-0000-4000-8000-000000000001",
      event_types: ["alert.transition"],
      active: true,
    },
    {
      id: "5a000000-0000-4000-8000-000000000002",
      event_types: ["run.transition", "alert.transition"],
      active: true,
    },
    {
      id: "5a000000-0000-4000-8000-000000000003",
      event_types: ["run.transition"],
      active: true,
    },
  ],
  next_cursor: null,
});
