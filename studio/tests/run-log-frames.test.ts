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
// The run log's called workflows: the steps that ran inside one show under
// the call that ran it, a call inside it under its own step, each with the
// status the host gives its run; and test output for a step inside one goes
// to that workflow's test data, where the next execution sends it.
import "@angular/compiler";
import { describe, expect, it } from "vitest";
import type { Workflow } from "../src/app/model";
import {
  calledNames,
  failedIn,
  frameLog,
  opensFirst,
  runStatus,
  takesTestOutput,
} from "../src/app/editor/debugger/run-log-panel";
import {
  executeRequest,
  splitCalls,
} from "../src/app/editor/state/execute-request";
import type {
  ExecuteResponse,
  FrameView,
  TraceEntry,
} from "../src/app/editor/state/execution";
import { withCalleeMock } from "../src/app/editor/state/test-data-ops";
import type { StudioTestData } from "../src/app/editor/state/test-data";

const call = (id: string, uses: string) => ({
  id,
  kind: "callWorkflow",
  uses,
  mode: "wait",
  with: { literal: {} },
});
const ORDER_INTAKE = {
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name: "order-intake", version: "1.2.0" },
  spec: {
    steps: [
      { id: "check", kind: "transform", value: { literal: {} } },
      call("notify", "notify-customer@1.0.0"),
      { id: "summarize", kind: "transform", value: { literal: {} } },
    ],
  },
} as unknown as Workflow;

let index = 0;
const ran = (
  nodeId: string,
  frame?: string,
  more: Partial<TraceEntry> = {},
): TraceEntry => ({
  index: index++,
  nodeId,
  instanceKey: "",
  kind: "transform",
  status: "completed",
  output: { text: "Hello" },
  ...(frame ? { frame } : {}),
  ...more,
});
const frame = (
  key: string,
  workflow: string,
  callNode: string,
  status: FrameView["status"] = "completed",
): FrameView => ({ key, workflow, callNode, status });
/** As the host answers order-intake → notify-customer → customer-profile. */
const answer = (
  trace: TraceEntry[],
  frames: FrameView[],
  more: Partial<ExecuteResponse> = {},
): ExecuteResponse => ({
  status: "completed",
  compile: { ok: true, sliced: false, diagnostics: [], stubs: [] },
  trace,
  frames,
  ...more,
});
const CALLED = answer(
  [
    ran("check"),
    ran("notify", undefined, { kind: "callWorkflow" }),
    ran("render", "notify"),
    ran("enrich", "notify", { kind: "callWorkflow" }),
    ran("lookup-profile", "notify/enrich", { output: [1, 2] }),
    ran("send", "notify", { kind: "action" }),
    ran("summarize"),
  ],
  [
    frame("notify", "notify-customer@1.0.0", "notify"),
    frame("notify/enrich", "customer-profile@2.1.0", "enrich"),
  ],
);
const log = (response: ExecuteResponse) =>
  frameLog(splitCalls(ORDER_INTAKE, response).called);

describe("called workflows in the run log", () => {
  it("shows the steps that ran inside a called workflow under the call that ran it", () => {
    const frames = log(CALLED);
    expect([...frames.keys()]).toEqual(["notify"]);
    const [notify] = frames.get("notify")!;
    expect(notify).toMatchObject({
      key: "notify",
      workflow: "notify-customer@1.0.0",
      name: "notify-customer",
      version: "1.0.0",
      item: null,
      status: "completed",
    });
    expect(notify.steps.map((step) => [step.id, step.summary])).toEqual([
      ["render", "1 field"],
      ["enrich", "1 field"],
      ["send", "1 field"],
    ]);
    // A call inside it shows under its own step, with the steps it ran.
    const [profile] = notify.steps[1].frames;
    expect([profile.key, profile.name, profile.version]).toEqual([
      "notify/enrich",
      "customer-profile",
      "2.1.0",
    ]);
    expect(profile.steps.map((step) => [step.id, step.summary])).toEqual([
      ["lookup-profile", "2 items"],
    ]);
    expect(notify.steps[0].frames).toEqual([]);
  });

  it("keeps each item's call apart in a loop, with the calls they made under them", () => {
    const frames = log(
      answer(
        [
          ran("notify", undefined, { instanceKey: "notify[0]" }),
          ran("enrich", "notify[0]"),
          ran("lookup-profile", "notify[0]/enrich"),
          ran("notify", undefined, { instanceKey: "notify[2]" }),
          ran("enrich", "notify[2]"),
          ran("lookup-profile", "notify[2]/enrich", { status: "failed" }),
        ],
        [
          frame("notify[0]", "notify-customer@1.0.0", "notify[0]"),
          frame("notify[0]/enrich", "customer-profile@2.1.0", "enrich"),
          frame("notify[2]", "notify-customer@1.0.0", "notify[2]", "failed"),
          frame(
            "notify[2]/enrich",
            "customer-profile@2.1.0",
            "enrich",
            "failed",
          ),
        ],
      ),
    );
    const items = frames.get("notify")!;
    expect(items.map((item) => [item.key, item.item, item.status])).toEqual([
      ["notify[0]", "Item 1", "completed"],
      ["notify[2]", "Item 3", "failed"],
    ]);
    const inner = items[1].steps[0].frames;
    expect(inner.map((item) => [item.key, item.status])).toEqual([
      ["notify[2]/enrich", "failed"],
    ]);
    expect(inner[0].steps.map((step) => [step.status, step.summary])).toEqual([
      ["failed", null],
    ]);
    expect(items[0].steps[0].frames[0].steps[0].status).toBe("completed");
  });

  it("takes a called workflow's status from its run, not from the steps listed", () => {
    // Every step listed finished, yet the run failed after them (its output didn't fit, say).
    const frames = log(
      answer(
        [ran("notify"), ran("render", "notify")],
        [frame("notify", "notify-customer@1.0.0", "notify", "failed")],
      ),
    );
    expect(frames.get("notify")![0].status).toBe("failed");
    expect(failedIn(frames.get("notify")!)).toBe("Failed in notify-customer");
    expect(failedIn(log(CALLED).get("notify")!)).toBeNull();
    const [done] = log(CALLED).get("notify")!;
    expect(failedIn([{ ...done, status: "waiting" }])).toBeNull();
  });

  it("lists a step that ran for several items inside a called workflow once, with how it last ended", () => {
    const frames = log(
      answer(
        [
          ran("notify"),
          ran("send", "notify", { instanceKey: "send[0]" }),
          ran("send", "notify", {
            instanceKey: "send[1]",
            status: "failed",
            output: undefined,
          }),
        ],
        [frame("notify", "notify-customer@1.0.0", "notify", "failed")],
      ),
    );
    expect(
      frames.get("notify")![0].steps.map((step) => [step.id, step.status]),
    ).toEqual([["send", "failed"]]);
  });

  it("opens a called workflow that failed or is still running, and keeps one that finished closed", () => {
    const [done] = log(CALLED).get("notify")!;
    expect(opensFirst(done)).toBe(false);
    for (const status of ["failed", "waiting", "running"] as const)
      expect(opensFirst({ ...done, status })).toBe(true);
  });

  it("marks the step inside a called workflow that needs test data, and says so in the status", () => {
    const blocked = answer(
      [
        ran("notify", undefined, { status: "waiting" }),
        ran("render", "notify"),
        ran("enrich", "notify", {
          kind: "callWorkflow",
          status: "waiting",
          output: undefined,
        }),
      ],
      [frame("notify", "notify-customer@1.0.0", "notify", "waiting")],
      {
        status: "blocked",
        blocked: {
          reason: "missing_mock",
          nodeId: "enrich",
          instanceKey: "",
          frame: "notify",
          message: "Step enrich in notify-customer needs test data.",
        },
      },
    );
    const { response, called } = splitCalls(ORDER_INTAKE, blocked);
    const [notify] = frameLog(called).get("notify")!;
    expect(notify.steps.map((step) => [step.id, step.needsData])).toEqual([
      ["render", false],
      ["enrich", true],
    ]);
    expect(runStatus(response, null, called)).toBe(
      "enrich in notify-customer needs test data",
    );
    // Test output is offered for it, a call though it is, and for tasks; not for a step that computes.
    expect(notify.steps.map(takesTestOutput)).toEqual([false, true]);
    expect(
      takesTestOutput({ ...notify.steps[0], kind: "action" }) &&
        takesTestOutput({ ...notify.steps[0], kind: "llm" }),
    ).toBe(true);
  });

  it("names the workflows that ran on this computer once each, in the order their calls started", () => {
    expect(calledNames(splitCalls(ORDER_INTAKE, CALLED).called)).toEqual([
      "notify-customer",
      "customer-profile",
    ]);
    const three = answer(
      [],
      [
        frame("notify[0]", "notify-customer@1.0.0", "notify[0]"),
        frame("notify[1]", "notify-customer@1.0.0", "notify[1]"),
        frame("notify[1]/enrich", "customer-profile@2.1.0", "enrich"),
        frame("bill", "billing-close@3.0.0", "bill"),
      ],
    );
    expect(calledNames(splitCalls(ORDER_INTAKE, three).called)).toEqual([
      "notify-customer",
      "customer-profile",
      "billing-close",
    ]);
    expect(calledNames(null)).toEqual([]);
  });
});

describe("test output for a step inside a called workflow", () => {
  const EMPTY: StudioTestData = {
    schemaVersion: 1,
    kind: "weave.studio/testData",
    updatedAt: "2026-11-18T10:00:00Z",
  };
  const NOW = "2026-11-18T12:00:00Z";

  it("goes to that workflow's test data, and the next execution sends it", () => {
    const first = withCalleeMock(
      EMPTY,
      "notify-customer@1.0.0",
      "send",
      { sent: true },
      NOW,
    );
    const data = withCalleeMock(
      first,
      "notify-customer@1.0.0",
      "enrich",
      { tier: "silver" },
      NOW,
    );
    expect(data.workflows).toEqual({
      "notify-customer@1.0.0": {
        mocks: {
          send: { output: { sent: true }, source: "manual" },
          enrich: { output: { tier: "silver" }, source: "manual" },
        },
      },
    });
    expect(data.updatedAt).toBe(NOW);
    expect(data.pins).toBeUndefined();
    // Setting it again replaces it.
    const again = withCalleeMock(
      data,
      "notify-customer@1.0.0",
      "send",
      { sent: false },
      NOW,
    );
    expect(again.workflows?.["notify-customer@1.0.0"].mocks["send"]).toEqual({
      output: { sent: false },
      source: "manual",
    });
    const request = executeRequest({
      source: "{}",
      format: "json",
      workflow: ORDER_INTAKE,
      definitions: [],
      connected: false,
      testData: again,
      results: null,
      sampleInput: {},
      target: { mode: "workflow" },
      now: new Date(NOW),
    });
    expect(request.workflows?.["notify-customer@1.0.0"].mocks).toEqual({
      "node:send": { sent: false },
      "node:enrich": { tier: "silver" },
    });
  });
});
