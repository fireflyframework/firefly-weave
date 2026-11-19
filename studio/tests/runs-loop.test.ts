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
// A loop in a recorded run: how many items it had and what each did, from
// the run's state and its step facts; the steps inside it in the item
// picked; one item's input recomputed from what the run recorded.
import { describe, expect, it } from "vitest";
import { loopResultText } from "../src/app/editor/canvas/tile-facts";
import { RunData } from "../src/app/editor/runs-tab/run-data";
import {
  defaultPick,
  failedFirst,
  overlayOf,
  picksFor,
  recordedLoops,
} from "../src/app/editor/runs-tab/run-overlay";
import {
  readRunRecord,
  type RunRecord,
} from "../src/app/editor/runs-tab/run-record";
import type { Step, Workflow } from "../src/app/model";
import type { StepFact } from "../src/app/operate/run-contracts";

const RUN = "3f2a9c1e-5b7d-4e8f-9a0b-1c2d3e4f5a6b";
const VERSION = "9e8f5b0c-cfee-4af4-8dea-d2ae9fb06b9a";
const ENVIRONMENT = "00000000-0000-4000-8000-000000000003";

const ORDERS = [
  { email: "a@x", lines: [{ sku: "a1" }, { sku: "a2" }] },
  { email: "b@x", lines: [{ sku: "b1" }] },
  { email: "c@x", lines: [] },
  { email: "d@x", lines: [] },
  { email: "e@x", lines: [] },
];

/** A runs.read answer for a run that failed at send on item 4 of 5. */
function runView(state: Record<string, unknown>): Record<string, unknown> {
  return {
    id: RUN,
    activation: {
      id: "a09f6c1d-d0ff-4b05-9efb-e3bfa0c17cab",
      revision: 1,
      name: "order-notify",
      request: {
        version_id: VERSION,
        artifact_digest: `sha256:${"a".repeat(64)}`,
        scope: {},
        connection_revision_ids: {},
        worker_release_ids: {},
      },
      retired: false,
    },
    state: {
      status: "failed",
      active: [],
      input: {},
      steps: { fetch: { output: { orders: ORDERS } } },
      waits: {},
      branches: {},
      joins: {},
      incidents: {},
      ...state,
    },
  };
}
const loopState = (count: number) => ({
  owner: "orders",
  join: "orders/join",
  count,
  concurrency: 2,
  status: "terminated",
});
const record = (state: Record<string, unknown> = {}): RunRecord =>
  readRunRecord(
    runView({
      loops: {
        orders: loopState(5),
        "lines[0]": { ...loopState(2), owner: "lines" },
      },
      incidents: {
        "send[3]:0": {
          node_id: "send[3]",
          code: "SMTP_REJECTED",
          generation: 0,
        },
      },
      ...state,
    }),
  )!;

function fact(
  key: string,
  status: StepFact["status"],
  output?: unknown,
): StepFact {
  const iteration = [...key.matchAll(/\[(\d+)\]/g)].map((m) => Number(m[1]));
  return {
    node_id: key.replace(/\[.*$/, ""),
    instance_key: key,
    iteration,
    kind: "action",
    status,
    scheduled_at: null,
    started_at: null,
    ended_at: null,
    duration_ms: null,
    queue_wait_ms: null,
    attempts: 1,
    worker_id: null,
    error_code: status === "failed" ? "SMTP_REJECTED" : null,
    child_run_id: null,
    log_entries: 0,
    ...(output === undefined ? {} : { output }),
  };
}
/** Items 1 to 3 finished, item 4 failed at send, item 5 never ran; lines ran inside items 1 and 2. */
const FACTS: StepFact[] = [
  fact("fetch", "succeeded"),
  ...[0, 1, 2, 3].map((i) => fact(`check[${i}]`, "succeeded")),
  ...[0, 1, 2].map((i) => fact(`send[${i}]`, "succeeded")),
  fact("send[3]", "failed"),
  fact("reserve[0][0]", "succeeded"),
  fact("reserve[0][1]", "succeeded"),
  fact("reserve[1][0]", "failed"),
  fact("orders", "failed"),
];

const action = (id: string, withValue: unknown): Step => ({
  id,
  kind: "action",
  action: "crm.lookup@2.0.0",
  with: withValue,
});
function workflow(send: unknown = { literal: {} }): Workflow {
  return {
    apiVersion: "weave/v1alpha1",
    kind: "Workflow",
    metadata: { name: "order-notify", version: "1.0.0" },
    spec: {
      steps: [
        { id: "fetch", kind: "transform", value: { literal: {} } },
        {
          id: "orders",
          kind: "forEach",
          items: { ref: "/steps/fetch/output/orders" },
          concurrency: 2,
          maxItems: 1000,
          collect: "all",
          body: {
            steps: [
              action("check", { literal: {} }),
              {
                id: "lines",
                kind: "forEach",
                items: { ref: "/item/lines" },
                concurrency: 1,
                maxItems: 1000,
                collect: "all",
                body: {
                  steps: [
                    action("reserve", {
                      object: {
                        sku: { ref: "/item/sku" },
                        to: { ref: "/loops/orders/item/email" },
                      },
                    }),
                  ],
                  output: { ref: "/item" },
                },
              },
              action("send", send),
            ],
            output: { ref: "/item" },
          },
        },
        { id: "summarize", kind: "transform", value: { literal: {} } },
      ],
    },
  } as Workflow;
}
const versions = (draft = workflow()) => ({
  run: null,
  draft,
  runVersion: "1.0.0",
  test: false,
});

describe("a recorded run's loops", () => {
  it("reads how many items each run of a loop had", () => {
    expect(record().loops).toEqual({ orders: 5, "lines[0]": 2 });
    expect(readRunRecord(runView({}))!.loops).toEqual({});
    // A count in another shape reads as nothing, like the run's other fields.
    expect(
      readRunRecord(runView({ loops: { orders: { count: "5" } } })),
    ).toBeNull();
  });

  it("lists each item of a loop with what it did, the failed ones first", () => {
    const [orders] = recordedLoops(workflow(), record(), FACTS);
    expect(orders).toMatchObject({ id: "orders", within: [], count: 5 });
    expect(orders.items).toEqual([
      { index: 0, status: "finished", code: null },
      // Its nested loop failed at reserve, so item 2 failed too.
      { index: 1, status: "failed", code: "SMTP_REJECTED" },
      { index: 2, status: "finished", code: null },
      { index: 3, status: "failed", code: "SMTP_REJECTED" },
      { index: 4, status: "not-run", code: null },
    ]);
    expect(failedFirst(orders).map((item) => item.index)).toEqual([
      1, 3, 0, 2, 4,
    ]);
    expect(defaultPick(orders)).toBe(1);
    // Without step facts, the run's incidents still say which item failed.
    const [bare] = recordedLoops(workflow(), record(), null);
    expect(bare.items.map((item) => item.status)).toEqual([
      "not-run",
      "not-run",
      "not-run",
      "failed",
      "not-run",
    ]);
    expect(bare.items[3].code).toBe("SMTP_REJECTED");
  });

  it("lists a nested loop once per item of the loop around it", () => {
    const lines = recordedLoops(workflow(), record(), FACTS).filter(
      (loop) => loop.id === "lines",
    );
    expect(lines.map(({ within, count }) => ({ within, count }))).toEqual([
      { within: [0], count: 2 },
      // No count recorded: the items the run reached.
      { within: [1], count: 1 },
    ]);
    expect(lines[1].items).toEqual([
      { index: 0, status: "failed", code: "SMTP_REJECTED" },
    ]);
    // With nothing failed, the first item is picked.
    expect(defaultPick(lines[0])).toBe(0);
  });

  it("opens each loop on its first failed item, a nested loop within the item picked around it", () => {
    const loops = recordedLoops(workflow(), record(), FACTS);
    // orders opens on item 2, where lines failed at its first item.
    expect(picksFor(workflow(), loops, {})).toEqual({ orders: 1, lines: 0 });
    // A pick stays; lines then opens on its first item in item 1.
    expect(picksFor(workflow(), loops, { orders: 0 })).toEqual({
      orders: 0,
      lines: 0,
    });
    // A pick past the end of the list goes back to its default.
    expect(picksFor(workflow(), loops, { orders: 7 })).toEqual({
      orders: 1,
      lines: 0,
    });
    // lines had two items in item 1, one in item 2.
    expect(picksFor(workflow(), loops, { orders: 1, lines: 1 })).toEqual({
      orders: 1,
      lines: 0,
    });
    expect(picksFor(workflow(), loops, { orders: 0, lines: 1 })).toEqual({
      orders: 0,
      lines: 1,
    });
  });

  it("draws the steps inside a loop in the picked item, the loop in the whole run", () => {
    const run = record();
    const aggregate = overlayOf(run, FACTS, versions());
    expect(aggregate.states.get("send")).toBe("failed");
    const second = overlayOf(run, FACTS, versions(), undefined, {
      orders: 1,
    });
    expect(second.states.get("check")).toBe("completed");
    expect(second.states.get("send")).toBe("completed");
    expect(second.states.get("reserve")).toBe("failed");
    // The loop keeps the run's state, failed at item 4.
    expect(second.states.get("orders")).toBe("failed");
    expect(second.canvas.failures).not.toHaveProperty("send");
    const last = overlayOf(run, FACTS, versions(), undefined, { orders: 4 });
    expect(last.states.get("check")).toBe("not-reached");
    expect(last.states.get("send")).toBe("not-reached");
    expect(last.canvas.done).toEqual(
      expect.not.arrayContaining(["check", "send"]),
    );
    // The canvas gets the loops and the picks, for the item chip.
    expect(second.canvas.loops?.map((loop) => loop.id)).toEqual([
      "orders",
      "lines",
      "lines",
    ]);
    expect(second.canvas.picks).toEqual({ orders: 1 });
  });

  it("says how many items a loop had and how many results it left out", () => {
    expect(loopResultText(20, 20)).toBe("20 items");
    expect(loopResultText(20, 18)).toBe("20 items, 2 skipped");
    expect(loopResultText(1, 1)).toBe("1 item");
    expect(loopResultText(0, 0)).toBe("No items");
    expect(loopResultText(5, null)).toBe("5 items");
  });
});

describe("one item's input in a recorded run", () => {
  const reads = {
    object: { to: { ref: "/item/email" }, position: { ref: "/index" } },
  };

  it("recomputes it from the loop's list in the run's data", () => {
    const data = new RunData(record(), ENVIRONMENT, workflow(reads));
    expect(data.received("send", "send[2]")).toEqual({
      ok: true,
      value: { to: "c@x", position: 2 },
    });
    // A nested loop's item, and the item of the loop around it.
    expect(data.received("reserve", "reserve[1][0]")).toEqual({
      ok: true,
      value: { sku: "b1", to: "b@x" },
    });
    // Without an item, past the end of the list, or without the list's step
    // in the run, it says why.
    const why = {
      ok: false,
      reason: "Studio can't recompute one iteration's input yet.",
    };
    expect(data.received("send")).toEqual(why);
    expect(data.received("send", "send[9]")).toEqual(why);
    const bare = new RunData(
      record({ steps: {} }),
      ENVIRONMENT,
      workflow(reads),
    );
    expect(bare.received("send", "send[2]")).toEqual(why);
  });

  it("reads the steps before it in the same item from the outputs the run recorded", () => {
    const data = new RunData(
      record(),
      ENVIRONMENT,
      workflow({ object: { ok: { ref: "/steps/check/output/ok" } } }),
    );
    expect(data.received("send", "send[1]")).toEqual({
      ok: false,
      reason: "Studio can't recompute one iteration's input yet.",
    });
    data.setOutputs([
      fact("check[0]", "succeeded", { ok: false }),
      fact("check[1]", "succeeded", { ok: true }),
    ]);
    expect(data.received("send", "send[1]")).toEqual({
      ok: true,
      value: { ok: true },
    });
  });
});
