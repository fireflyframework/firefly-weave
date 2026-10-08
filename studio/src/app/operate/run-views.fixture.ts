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
// Deterministic run views for building and testing against run_summaries.list,
// runs.steps and runs.logs before a platform serves them. The fake in
// run-views-fake.ts answers requests from this data.
import type { RunLogEntry, RunSummary, StepFact } from "./run-contracts";

export interface FixtureRun {
  summary: RunSummary;
  /** Listed as an unavailable placeholder; filters still see its facts. */
  unavailable?: true;
}
export interface RunViewsFixture {
  runs: FixtureRun[];
  /** Step facts per run. Outputs are stored here and returned only with include=output. */
  steps: Record<string, { complete: boolean; items: StepFact[] }>;
  logs: Record<string, RunLogEntry[]>;
}

export const fixtureIds = {
  succeeded: "0b9c6f7e-3c55-4d6b-9a51-4f1b0c2d7e01",
  failed: "1c0d7f8a-4d66-4e7c-8b62-5a2c1d3e8f12",
  child: "2d1e8a9b-5e77-4f8d-9c73-6b3d2e4f9a23",
  retry: "3e2f9bac-6f88-4a9e-8d84-7c4e3f5a0b34",
  running: "4f3a0cbd-7a99-4baf-9e95-8d5f4a6b1c45",
  test: "5a4b1dce-8baa-4cb0-8fa6-9e6a5b7c2d56",
  testChild: "6b5c2edf-9cbb-4dc1-9ab7-af7b6c8d3e67",
  archived: "7c6d3fea-adcc-4ed2-8bc8-b08c7d9e4f78",
  unavailable: "8d7e4afb-bedd-4fe3-9cd9-c19d8eaf5a89",
} as const;
const invoice = {
  name: "invoice-approval",
  version: "1.3.0",
  definition_version_id: "9e8f5b0c-cfee-4af4-8dea-d2ae9fb06b9a",
};
const invoiceActivation = "a09f6c1d-d0ff-4b05-9efb-e3bfa0c17cab";
const notify = {
  name: "notify-customer",
  version: "2.0.0",
  definition_version_id: "b1a07d2e-e100-4c16-8f0c-f4c0b1d28dbc",
};
const notifyActivation = "c2b18e3f-f211-4d27-9a1d-05d1c2e39ecd";
const testActivation = "d3c29f40-0322-4e38-8b2e-16e2d3f4afde";
const workers = {
  http: "e4d3a051-1433-4f49-9c3f-27f3e405b0ef",
  mail: "f5e4b162-2544-4a5a-8d40-38a4f516c1f0",
  agent: "06f5c273-3655-4b6b-9e51-49b50627d201",
};

function run(
  id: string,
  facts: Partial<RunSummary> & Pick<RunSummary, "status" | "started_at">,
): RunSummary {
  return {
    id,
    workflow: invoice,
    activation_id: invoiceActivation,
    activation: { name: "invoice-approval", revision: 4 },
    paused: false,
    test: false,
    origin: "webhook",
    caller: null,
    retried_from_run_id: null,
    updated_at: facts.ended_at ?? facts.started_at,
    ended_at: null,
    duration_ms: null,
    business_key: null,
    correlation_key: null,
    failed_step: null,
    active_incidents: 0,
    archived: false,
    ...facts,
  };
}
function step(
  facts: Partial<StepFact> & Pick<StepFact, "node_id" | "kind" | "status">,
): StepFact {
  return {
    instance_key: "",
    iteration: [],
    scheduled_at: null,
    started_at: null,
    ended_at: null,
    duration_ms: null,
    queue_wait_ms: null,
    attempts: 0,
    worker_id: null,
    error_code: null,
    child_run_id: null,
    log_entries: 0,
    ...facts,
  };
}
function timed(
  scheduled: string,
  started: string,
  ended: string | null,
): Pick<
  StepFact,
  "scheduled_at" | "started_at" | "ended_at" | "duration_ms" | "queue_wait_ms"
> {
  const at = (value: string) => Date.parse(`2026-10-07T${value}Z`);
  return {
    scheduled_at: `2026-10-07T${scheduled}Z`,
    started_at: `2026-10-07T${started}Z`,
    ended_at: ended === null ? null : `2026-10-07T${ended}Z`,
    duration_ms: ended === null ? null : at(ended) - at(started),
    queue_wait_ms: at(started) - at(scheduled),
  };
}
function log(
  id: string,
  facts: Partial<RunLogEntry> &
    Pick<RunLogEntry, "at" | "source" | "level" | "code" | "message">,
): RunLogEntry {
  return {
    id,
    node_id: null,
    instance_key: "",
    attempt: null,
    worker_id: null,
    fields: {},
    redactions: 0,
    truncated: false,
    ...facts,
  };
}

export const runViewsFixture: RunViewsFixture = {
  runs: [
    {
      summary: run(fixtureIds.succeeded, {
        status: "succeeded",
        started_at: "2026-10-07T09:00:00Z",
        ended_at: "2026-10-07T09:00:42Z",
        duration_ms: 42000,
        business_key: "INV-1040",
      }),
    },
    {
      summary: run(fixtureIds.failed, {
        status: "failed",
        origin: "schedule",
        started_at: "2026-10-07T10:00:00Z",
        ended_at: "2026-10-07T10:01:05Z",
        duration_ms: 65000,
        business_key: "INV-1041",
        failed_step: {
          node_id: "send",
          instance_key: "send[3]",
          error_code: "SMTP_REJECTED",
        },
        active_incidents: 1,
      }),
    },
    {
      summary: run(fixtureIds.child, {
        workflow: notify,
        activation_id: notifyActivation,
        activation: { name: "notify-customer", revision: 2 },
        status: "succeeded",
        origin: "call",
        caller: {
          run_id: fixtureIds.failed,
          node_id: "notify",
          instance_key: "",
        },
        started_at: "2026-10-07T10:00:07Z",
        ended_at: "2026-10-07T10:00:29Z",
        duration_ms: 22000,
        business_key: "INV-1041",
      }),
    },
    {
      summary: run(fixtureIds.retry, {
        status: "succeeded",
        origin: "retry",
        retried_from_run_id: fixtureIds.failed,
        started_at: "2026-10-07T11:00:00Z",
        ended_at: "2026-10-07T11:00:50Z",
        duration_ms: 50000,
        business_key: "INV-1041",
      }),
    },
    {
      summary: run(fixtureIds.running, {
        status: "running",
        started_at: "2026-10-07T11:30:00Z",
        updated_at: "2026-10-07T11:30:05Z",
        business_key: "INV-1042",
      }),
    },
    {
      summary: run(fixtureIds.test, {
        workflow: { ...invoice, version: "1.3.0+test.d3c29f400322" },
        activation_id: testActivation,
        activation: { name: "invoice-approval", revision: 1 },
        status: "waiting",
        test: true,
        origin: "test",
        started_at: "2026-10-07T11:40:00Z",
        updated_at: "2026-10-07T11:40:12Z",
      }),
    },
    {
      summary: run(fixtureIds.testChild, {
        workflow: notify,
        activation_id: notifyActivation,
        activation: { name: "notify-customer", revision: 2 },
        status: "succeeded",
        test: true,
        origin: "call",
        caller: {
          run_id: fixtureIds.test,
          node_id: "notify",
          instance_key: "",
        },
        started_at: "2026-10-07T11:40:05Z",
        ended_at: "2026-10-07T11:40:11Z",
        duration_ms: 6000,
      }),
    },
    {
      summary: run(fixtureIds.archived, {
        status: "cancelled",
        origin: "manual",
        started_at: "2026-10-06T08:00:00Z",
        ended_at: "2026-10-06T08:05:00Z",
        duration_ms: 300000,
        archived: true,
      }),
    },
    {
      summary: run(fixtureIds.unavailable, {
        status: "succeeded",
        origin: "manual",
        started_at: "2026-10-07T08:00:00Z",
        ended_at: "2026-10-07T08:00:10Z",
        duration_ms: 10000,
      }),
      unavailable: true,
    },
  ],
  steps: {
    // Recorded before the upgrade: no timings, so the list is incomplete.
    [fixtureIds.succeeded]: {
      complete: false,
      items: [
        step({
          node_id: "fetch",
          kind: "action",
          status: "succeeded",
          output: { total: 98 },
        }),
        step({
          node_id: "approve",
          kind: "humanTask",
          status: "succeeded",
          output: { approved: true },
        }),
      ],
    },
    [fixtureIds.failed]: {
      complete: true,
      items: [
        step({
          node_id: "fetch",
          kind: "action",
          status: "succeeded",
          ...timed("10:00:01", "10:00:02", "10:00:05"),
          attempts: 1,
          worker_id: workers.http,
          log_entries: 1,
          output: { invoice: { id: "INV-1041", total: 120.5 } },
        }),
        step({
          node_id: "notify",
          kind: "callWorkflow",
          status: "succeeded",
          ...timed("10:00:06", "10:00:07", "10:00:30"),
          attempts: 1,
          child_run_id: fixtureIds.child,
          output: { delivered: true },
        }),
        step({
          node_id: "items",
          kind: "forEach",
          status: "failed",
          ...timed("10:00:31", "10:00:31", "10:01:05"),
          attempts: 1,
          error_code: "SMTP_REJECTED",
        }),
        step({
          node_id: "send",
          instance_key: "send[0]",
          iteration: [0],
          kind: "action",
          status: "succeeded",
          ...timed("10:00:31", "10:00:32", "10:00:33"),
          attempts: 1,
          worker_id: workers.mail,
          output: null,
        }),
        step({
          node_id: "send",
          instance_key: "send[1]",
          iteration: [1],
          kind: "action",
          status: "succeeded",
          ...timed("10:00:31", "10:00:33", "10:00:35"),
          attempts: 1,
          worker_id: workers.mail,
          output: { message_id: "m-1" },
        }),
        step({
          node_id: "send",
          instance_key: "send[2]",
          iteration: [2],
          kind: "action",
          status: "succeeded",
          ...timed("10:00:31", "10:00:35", "10:00:36"),
          attempts: 1,
          worker_id: workers.mail,
          omissions: [{ path: "/output", reason: "classified_secret" }],
        }),
        step({
          node_id: "send",
          instance_key: "send[3]",
          iteration: [3],
          kind: "action",
          status: "failed",
          ...timed("10:00:31", "10:00:36", "10:01:04"),
          attempts: 3,
          worker_id: workers.mail,
          error_code: "SMTP_REJECTED",
          log_entries: 3,
        }),
      ],
    },
    [fixtureIds.running]: {
      complete: true,
      items: [
        step({
          node_id: "support",
          kind: "agent",
          status: "running",
          ...timed("11:30:01", "11:30:01", null),
          attempts: 1,
          worker_id: workers.agent,
          log_entries: 0,
        }),
        step({
          node_id: "support",
          instance_key: "support#1",
          kind: "agent",
          status: "succeeded",
          ...timed("11:30:01", "11:30:01", "11:30:03"),
          attempts: 1,
          worker_id: workers.agent,
          log_entries: 2,
          output: { reply: "Your invoice is approved." },
        }),
        step({
          node_id: "support",
          instance_key: "support#2",
          kind: "agent",
          status: "running",
          ...timed("11:30:04", "11:30:04", null),
          attempts: 1,
          worker_id: workers.agent,
          log_entries: 1,
        }),
      ],
    },
  },
  logs: {
    [fixtureIds.failed]: [
      log("17a6d384-4766-4c7c-8f62-5ac61738e312", {
        at: "2026-10-07T10:00:00Z",
        source: "engine",
        level: "info",
        code: "started",
        message: "",
      }),
      log("28b7e495-5877-4d8d-9a73-6bd72849f423", {
        at: "2026-10-07T10:00:04Z",
        source: "connector",
        level: "info",
        code: "HTTP.REQUEST",
        message: "GET api.example.com answered 200 in 840 ms",
        node_id: "fetch",
        attempt: 1,
        worker_id: workers.http,
        fields: {
          method: "GET",
          host: "api.example.com",
          status: 200,
          duration_ms: 840,
        },
      }),
      log("39c8f5a6-6988-4e9e-8b84-7ce8395a0534", {
        at: "2026-10-07T10:00:40Z",
        source: "worker",
        level: "warning",
        code: "SMTP.RETRY",
        message: "The mail server rejected the message; retrying",
        node_id: "send",
        instance_key: "send[3]",
        attempt: 2,
        worker_id: workers.mail,
        fields: { recipient: "[redacted]" },
        redactions: 1,
      }),
      log("4ad906b7-7a99-4faf-9c95-8df94a6b1645", {
        at: "2026-10-07T10:01:03Z",
        source: "worker",
        level: "error",
        code: "SMTP_REJECTED",
        message: "The mail server rejected the message",
        node_id: "send",
        instance_key: "send[3]",
        attempt: 3,
        worker_id: workers.mail,
      }),
      log("5bea17c8-8baa-4ab0-8da6-9e0a5b7c2756", {
        at: "2026-10-07T10:01:04Z",
        source: "engine",
        level: "error",
        code: "task_failed",
        message: "",
        node_id: "send",
        instance_key: "send[3]",
      }),
    ],
    [fixtureIds.running]: [
      log("6cfb28d9-9cbb-4bc1-8eb7-af1b6c8d3867", {
        at: "2026-10-07T11:30:02Z",
        source: "worker",
        level: "debug",
        code: "AGENT.PROMPT",
        message: "Prompt assembled",
        node_id: "support",
        instance_key: "support#1",
        attempt: 1,
        worker_id: workers.agent,
      }),
      log("7d0c39ea-adcc-4cd2-9fc8-b02c7d9e4978", {
        at: "2026-10-07T11:30:03Z",
        source: "ai",
        level: "info",
        code: "AI.USAGE",
        message: "AI turn finished",
        node_id: "support",
        instance_key: "support#1",
        attempt: 1,
        worker_id: workers.agent,
        fields: { input_tokens: 812, output_tokens: 96 },
      }),
      log("8e1d4afb-bedd-4de3-8ad9-c13d8eaf5a89", {
        at: "2026-10-07T11:30:05Z",
        source: "ai",
        level: "info",
        code: "AI.USAGE",
        message: "AI turn finished",
        node_id: "support",
        instance_key: "support#2",
        attempt: 1,
        worker_id: workers.agent,
        fields: { input_tokens: 1004, output_tokens: 41 },
      }),
    ],
  },
};
