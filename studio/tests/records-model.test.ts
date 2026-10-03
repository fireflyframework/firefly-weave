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
import { describe, expect, it } from "vitest";
import {
  columns,
  connectorLabel,
  contextFacts,
  countLabel,
  fieldLabel,
  finishedSteps,
  rowSecondary,
  rowStatus,
  rowTitle,
  runDuration,
  runIncident,
  runNow,
  runWorkflow,
  stepProgress,
  stepSummary,
  timeline,
} from "../src/app/operations/records-model";

const uuid = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;
const versionId = "11111111-1111-4111-8111-111111111111";
const versions = new Map([
  [versionId, { name: "expense-review", version: "1.0.0" }],
]);
const run = (overrides: Record<string, unknown> = {}) => ({
  id: "700587c4-1111-4111-8111-111111111111",
  business_key: "expense-104",
  activation: { name: "expense-review", request: { version_id: versionId } },
  state: { status: "waiting", active: ["review"] },
  ...overrides,
});

describe("list columns and rows", () => {
  it("gives each view its own columns", () => {
    expect(columns.workflows).toEqual([
      "Workflow",
      "Version",
      "Status",
      "Updated",
    ]);
    expect(columns.runs).toEqual(["Run", "Status", "Started", "Duration"]);
    expect(columns.tasks).toEqual(["Task", "Status", "Due"]);
    expect(columns.connections).toEqual([
      "Connection",
      "Connector",
      "Revision",
    ]);
    expect(columns.email).toEqual(["Conversation", "Last message", "Status"]);
    expect(columns.workers).toEqual(["Worker", "Status", "Registered limit"]);
  });

  it("counts in words", () => {
    expect(countLabel("runs", 3)).toBe("3 runs");
    expect(countLabel("tasks", 1)).toBe("1 task");
    expect(countLabel("email", 2)).toBe("2 conversations");
  });

  it("titles a run by its workflow and version, never by a UUID twice", () => {
    expect(rowTitle("runs", run(), { versions })).toBe("expense-review 1.0.0");
    expect(rowSecondary("runs", run(), { versions })).toBe("expense-104");
    const bare = run({ activation: undefined, business_key: undefined });
    expect(rowTitle("runs", bare)).toBe("Run 700587c4");
    expect(rowSecondary("runs", bare)).toBe("");
    const keyed = run({ activation: undefined });
    expect(rowTitle("runs", keyed)).toBe("expense-104");
    expect(rowSecondary("runs", keyed)).toBe("Run 700587c4");
    for (const record of [run(), bare, keyed])
      expect(rowSecondary("runs", record, { versions })).not.toMatch(uuid);
    expect(runWorkflow(run())).toBe("expense-review");
  });

  it("shows statuses, not versions or adapters, in the status column", () => {
    expect(rowStatus("runs", run({ state: { status: "failed" } }))).toEqual({
      label: "Failed",
      tone: "danger",
    });
    const published = { id: versionId, name: "pets", version: "1.0.0" };
    const status = rowStatus("workflows", published, {
      library: "workflows",
      activations: new Map([[versionId, {}]]),
    })!;
    expect(status.label).not.toMatch(/^\d+\.\d+\.\d+$/);
    expect(status).toEqual({ label: "Active", tone: "success" });
    expect(
      rowStatus("workflows", published, { library: "workflows" })!.label,
    ).toBe("Published");
    expect(
      rowSecondary("workflows", published, {
        library: "workflows",
        activations: new Map([[versionId, {}]]),
        environment: "Production",
      }),
    ).toBe("Active in Production");
    expect(rowSecondary("workflows", published, { library: "workflows" })).toBe(
      "Not activated",
    );
    expect(rowStatus("connections", { adapter: "weave-http-v2" })).toBeNull();
    expect(
      rowStatus(
        "tasks",
        { status: "claimed", claimant_id: "me" },
        {
          principal: "me",
        },
      )!.label,
    ).toBe("Claimed by you");
    expect(rowStatus("tasks", { status: "ready" })!.label).toBe(
      "Ready to claim",
    );
  });

  it("does not infer worker liveness from registration", () => {
    expect(rowStatus("workers", { revoked: false, capacity: 8 })).toEqual({
      label: "Registered",
      tone: "neutral",
    });
    expect(rowStatus("workers", { revoked: true, capacity: 8 })).toEqual({
      label: "Revoked",
      tone: "danger",
    });
    expect(rowStatus("workers", { unavailable: true })).toEqual({
      label: "Unavailable",
      tone: "danger",
    });
  });

  it("reads connectors and workers in words", () => {
    expect(connectorLabel("weave-http@2.0.0")).toBe("weave-http 2.0.0");
    expect(rowSecondary("connections", { connector: "weave-http@2.0.0" })).toBe(
      "weave-http 2.0.0",
    );
    const worker = {
      id: "7c9e6679-7425-40de-944b-e07fc1f90ae7",
      task_types: ["crm-lookup", "email-send"],
    };
    expect(rowTitle("workers", worker)).toBe("Worker 7c9e6679");
    expect(rowSecondary("workers", worker)).toBe("crm-lookup, email-send");
  });

  it("measures how long a run took", () => {
    const now = new Date("2026-10-01T09:14:05Z");
    expect(runDuration(run({ created_at: "2026-10-01T09:12:00Z" }), now)).toBe(
      "2 min 5 s",
    );
    expect(
      runDuration(
        run({
          created_at: "2026-10-01T09:12:00Z",
          state: { status: "succeeded" },
        }),
        now,
      ),
    ).toBe("");
  });
});

describe("run detail", () => {
  const steps = [
    { id: "check", kind: "transform" },
    { id: "review", kind: "humanTask", assignment: "reviewers" },
    { id: "pause", kind: "wait" },
  ];

  it("says what the run waits for and offers the task", () => {
    expect(runNow(run(), steps, { title: "Review expense 104" })).toEqual({
      lead: "Waiting for",
      subject: "Review expense 104",
      rest: ", assigned to reviewers",
      action: "task",
    });
    expect(runNow(run(), steps, null)).toMatchObject({
      lead: "Waiting for a person at",
      subject: "review",
      action: "tasks",
    });
    expect(
      runNow(
        run({ state: { status: "waiting", active: ["pause"] } }),
        steps,
        null,
      ),
    ).toMatchObject({ lead: "Waiting for time at", subject: "pause" });
    expect(
      runNow(
        run({
          state: { status: "waiting", active: ["x"], manual_paused: true },
        }),
        steps,
        null,
      )?.lead,
    ).toBe("Paused by an operator.");
    expect(
      runNow(run({ state: { status: "succeeded" } }), steps, null),
    ).toBeNull();
  });

  it("explains where a run stopped", () => {
    expect(
      runIncident(
        run({
          state: {
            status: "waiting",
            incident: "The CRM answered 500 three times.",
            incidents: { a: { node_id: "check", code: "WV-TASK" } },
          },
        }),
      ),
    ).toEqual({ step: "check", text: "The CRM answered 500 three times." });
    expect(runIncident(run())).toBeNull();
  });

  it("marks the live step, finished steps and the rest", () => {
    const history = {
      events: [
        { type: "started", timestamp: "2026-10-01T09:12:00Z", data: {} },
        {
          type: "task_completed",
          timestamp: "2026-10-01T09:12:01Z",
          data: { node_id: "check" },
        },
      ],
    };
    const finished = finishedSteps(history);
    expect([...finished]).toEqual(["check"]);
    expect(stepProgress(run(), "review", finished)).toBe("live");
    expect(stepProgress(run(), "check", finished)).toBe("done");
    expect(stepProgress(run(), "pause", finished)).toBe("unreached");
    expect(
      stepProgress(
        run({ state: { status: "waiting", steps: { pause: 1 } } }),
        "pause",
      ),
    ).toBe("done");
    expect(stepSummary(run(), steps[1], finished)).toBe(
      "review: the run is here",
    );
  });

  it("writes the history as sentences", () => {
    const rows = timeline({
      events: [
        { type: "run.started", at: "2026-10-01T09:12:00Z" },
        { type: "step.completed", node: "check", at: "2026-10-01T09:12:01Z" },
        {
          type: "human_task.created",
          node: "review",
          at: "2026-10-01T09:12:02Z",
        },
        { type: "incident_opened", data: { node_id: "check" }, timestamp: "x" },
        { type: "cancelled", timestamp: "2026-10-01T09:13:00Z" },
        { type: "something_new", timestamp: "2026-10-01T09:13:00Z" },
      ],
    });
    expect(rows.map((r) => r.sentence)).toEqual([
      "Run started",
      "check finished",
      "Waiting for a person at review",
      "Stopped at check",
      "Run canceled",
      "Something new",
    ]);
    expect(rows.map((r) => r.tone)).toEqual([
      "neutral",
      "success",
      "warning",
      "danger",
      "neutral",
      "neutral",
    ]);
    expect(rows[0].at).toBe("2026-10-01T09:12:00Z");
    expect(timeline(null)).toEqual([]);
  });
});

describe("task context", () => {
  it("lists label and value pairs and keeps nested data for later", () => {
    const { facts, nested } = contextFacts({
      amount: 125,
      currency: "EUR",
      submittedBy: "jane@acme.example",
      lines: [{ item: "Taxi" }],
    });
    expect(facts).toEqual([
      { label: "Amount", value: "€125.00" },
      { label: "Submitted by", value: "jane@acme.example" },
    ]);
    expect(nested).toEqual({ lines: [{ item: "Taxi" }] });
    expect(fieldLabel("due_at")).toBe("Due at");
  });
});
