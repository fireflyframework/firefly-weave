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
// A loop's progress while Execute workflow plays back on the canvas: how
// many items finished at each step of the playback, then how it ended.
import "@angular/compiler";
import { describe, expect, it } from "vitest";
import { loopRunText } from "../src/app/editor/canvas/loop-frame";
import {
  loopProgress,
  type TraceStep,
} from "../src/app/editor/canvas/run-state";
import {
  finalAnnouncement,
  playbackFrames,
} from "../src/app/editor/canvas/trace-playback";
import type { ExecuteResponse } from "../src/app/editor/state/execution";
import { loopLog, runStatus } from "../src/app/editor/debugger/run-log-panel";

const step = (
  nodeId: string,
  instanceKey = "",
  status: TraceStep["status"] = "completed",
  kind = "transform",
): TraceStep => ({ nodeId, instanceKey, status, kind });
/** fetch, then orders over three items two at a time (check, send), then summarize: as the simulator answers. */
const OK: TraceStep[] = [
  step("fetch"),
  // Skip empty results left one of three out.
  { ...step("orders", "", "completed", "forEach"), results: 2 },
  step("check", "check[0]"),
  step("check", "check[1]"),
  step("send", "send[0]"),
  step("send", "send[1]"),
  step("check", "check[2]"),
  step("send", "send[2]"),
  step("summarize"),
];
/** The steps inside each loop: check and send in orders; reserve in lines, in orders. */
const around = (id: string): readonly string[] =>
  ({
    check: ["orders"],
    send: ["orders"],
    lines: ["orders"],
    reserve: ["orders", "lines"],
  })[id] ?? [];

describe("a loop's progress during a playback", () => {
  it("counts the items finished at each step, then says it is done", () => {
    const at = (index: number) =>
      loopProgress(OK, index, { orders: 3 }, around, false);
    // Nothing is finished while the first items are at check.
    expect(at(3)).toEqual([
      {
        id: "orders",
        within: [],
        finished: 0,
        count: 3,
        status: "running",
        failedAt: null,
        results: null,
      },
    ]);
    // Item 1 is finished once its send has played.
    expect(at(5)[0]).toMatchObject({ finished: 1, status: "running" });
    expect(at(8)[0]).toMatchObject({ finished: 3, status: "done" });
    expect(loopProgress(OK, OK.length, { orders: 3 }, around, true)).toEqual([
      {
        id: "orders",
        within: [],
        finished: 3,
        count: 3,
        status: "done",
        failedAt: null,
        results: 2,
      },
    ]);
    // Before the loop starts, it has no progress.
    expect(loopProgress(OK, 0, { orders: 3 }, around, false)).toEqual([]);
  });

  it("names the first item that failed when several did", () => {
    const twice: TraceStep[] = [
      ...OK.slice(0, 4),
      step("send", "send[1]", "failed"),
      step("send", "send[0]", "failed"),
    ];
    expect(
      loopProgress(twice, twice.length, {}, around, true)[0].failedAt,
    ).toBe(1);
  });

  it("fails a loop that failed itself before its first item, and keeps why", () => {
    const over: TraceStep[] = [
      step("fetch"),
      {
        ...step("orders", "", "failed", "forEach"),
        code: "WV-RUNTIME-LOOP-LIMIT",
      },
    ];
    expect(loopProgress(over, over.length, {}, around, true)[0]).toMatchObject({
      status: "failed",
      finished: 0,
      failedAt: null,
      overLimit: true,
    });
    // Not yet while its entry plays.
    expect(loopProgress(over, 1, {}, around, false)[0].status).toBe("running");
    // Without a code the reason isn't known.
    const failed = [step("fetch"), step("orders", "", "failed", "forEach")];
    const run = loopProgress(failed, failed.length, {}, around, true)[0];
    expect(run).toMatchObject({ status: "failed", failedAt: null });
    expect(run.overLimit).toBeUndefined();
    // A loop whose items ran before a later step failed didn't fail itself.
    const later: TraceStep[] = [
      ...OK.slice(0, -1).map((entry) =>
        entry.kind === "forEach"
          ? { ...entry, status: "failed" as const }
          : entry,
      ),
      step("summarize", "", "failed"),
    ];
    expect(
      loopProgress(later, later.length, { orders: 3 }, around, true)[0],
    ).toMatchObject({ status: "done", failedAt: null, finished: 3 });
    // The frame's header says it.
    expect(
      loopRunText(loopProgress(over, over.length, {}, around, true)[0]),
    ).toBe("failed: list longer than Up to");
    expect(loopRunText(run)).toBe("failed");
  });

  it("says which item failed once its failure has played", () => {
    const failed: TraceStep[] = [
      ...OK.slice(0, 5),
      step("send", "send[1]", "failed"),
    ];
    // While the failing step plays, the item is still at work.
    expect(
      loopProgress(failed, 5, { orders: 3 }, around, false)[0],
    ).toMatchObject({ status: "running", finished: 1, failedAt: null });
    expect(
      loopProgress(failed, failed.length, {}, around, true)[0],
    ).toMatchObject({
      status: "failed",
      finished: 1,
      failedAt: 2,
      // The simulator gives no count for a loop that didn't end.
      count: null,
    });
  });

  it("follows a nested loop in each item of the loop around it", () => {
    const nested: TraceStep[] = [
      step("orders", "", "completed", "forEach"),
      step("check", "check[0]"),
      step("lines", "lines[0]", "completed", "forEach"),
      step("reserve", "reserve[0][0]"),
      step("reserve", "reserve[0][1]"),
      step("send", "send[0]"),
    ];
    const end = loopProgress(
      nested,
      nested.length,
      { orders: 1, "lines[0]": 2 },
      around,
      true,
    );
    expect(
      end.map(({ id, within, finished, count, status }) => ({
        id,
        within,
        finished,
        count,
        status,
      })),
    ).toEqual([
      { id: "orders", within: [], finished: 1, count: 1, status: "done" },
      { id: "lines", within: [0], finished: 2, count: 2, status: "done" },
    ]);
    // Item 1 of orders is still at work while its nested loop runs.
    expect(
      loopProgress(nested, 4, { orders: 1, "lines[0]": 2 }, around, false)[0],
    ).toMatchObject({ finished: 0, status: "running" });
    // The second line fails: the inner loop failed at item 2, and the outer at item 1.
    const failing: TraceStep[] = [
      step("orders", "", "failed", "forEach"),
      step("check", "check[0]"),
      step("lines", "lines[0]", "failed", "forEach"),
      step("reserve", "reserve[0][0]"),
      step("reserve", "reserve[0][1]", "failed"),
    ];
    const failed = loopProgress(
      failing,
      failing.length,
      { orders: 1, "lines[0]": 2 },
      around,
      true,
    );
    expect(
      failed.map(({ id, within, status, failedAt }) => ({
        id,
        within,
        status,
        failedAt,
      })),
    ).toEqual([
      { id: "orders", within: [], status: "failed", failedAt: 1 },
      { id: "lines", within: [0], status: "failed", failedAt: 2 },
    ]);
  });
});

describe("the playback's frames", () => {
  const response = (trace: TraceStep[], status: ExecuteResponse["status"]) =>
    ({
      status,
      compile: { ok: true, sliced: false, diagnostics: [], stubs: [] },
      trace: trace.map(({ results, ...entry }, index) => ({
        index,
        ...entry,
        ...(results === undefined
          ? {}
          : { output: Array.from({ length: results }, () => ({})) }),
      })),
      loops: [
        {
          nodeId: "orders",
          instanceKey: "",
          count: 3,
          completed: 3,
          running: 0,
          nextIndex: 3,
          status: "completed",
        },
      ],
    }) as ExecuteResponse;

  it("carry where the playback is in the trace and each loop's count", () => {
    const frames = playbackFrames(response(OK, "completed"), false);
    expect(frames).toHaveLength(OK.length + 1);
    expect(frames[3].trace?.length).toBe(OK.length);
    expect(frames[3].at).toBe(3);
    expect(frames[3].counts).toEqual({ orders: 3 });
    // The loop's entry says how many results its output list holds.
    expect(frames[3].trace?.[1].results).toBe(2);
    // The last frame is the whole run.
    expect(frames.at(-1)?.at).toBe(OK.length);
    expect(frames.at(-1)?.ended).toBe(true);
  });

  it("names the item a loop failed at when the run ends", () => {
    const failed = [...OK.slice(0, 5), step("send", "send[1]", "failed")];
    expect(finalAnnouncement(response(failed, "failed"), around)).toBe(
      "orders failed at item 2.",
    );
    // Without the workflow, the trace says which loop each step is in.
    expect(finalAnnouncement(response(failed, "failed"))).toBe(
      "orders failed at item 2.",
    );
  });

  it("says a loop failed before its first item, and why when the run says", () => {
    const over = [step("fetch"), step("orders", "", "failed", "forEach")];
    const answer = (codes: string[]) =>
      ({
        ...response(over, "failed"),
        loops: [],
        diagnostics: codes.map((code) => ({
          code,
          severity: "error",
          stage: "evaluation",
          message: "A loop's list was longer than its item limit.",
          path: "",
        })),
      }) as ExecuteResponse;
    expect(finalAnnouncement(answer(["WV-RUNTIME-LOOP-LIMIT"]), around)).toBe(
      "orders failed: its list is longer than Up to.",
    );
    // The frame reads the same reason from the playback.
    expect(
      playbackFrames(answer(["WV-RUNTIME-LOOP-LIMIT"]), true).at(-1)
        ?.trace?.[1],
    ).toMatchObject({ code: "WV-RUNTIME-LOOP-LIMIT" });
    expect(finalAnnouncement(answer([]), around)).toBe("orders failed.");
  });
});

describe("the run log of a loop", () => {
  const traced = (trace: TraceStep[]) =>
    trace.map((entry, index) => ({ index, ...entry }));
  /** Four orders one at a time; the third's path ends in reject. */
  const REJECTED: TraceStep[] = [
    step("fetch"),
    step("orders", "", "running", "forEach"),
    ...[0, 1].flatMap((i) => [
      step("check", `check[${i}]`),
      step("route", `route[${i}]`, "completed", "switch"),
      step("send", `send[${i}]`),
    ]),
    step("check", "check[2]"),
    step("route", "route[2]", "running", "switch"),
    step("reject", "reject[2]", "failed", "fail"),
  ];
  const inOrders = (id: string): readonly string[] =>
    ["check", "route", "send", "reject"].includes(id) ? ["orders"] : around(id);
  const failed = {
    status: "failed",
    compile: { ok: true, sliced: false, diagnostics: [], stubs: [] },
    trace: traced(REJECTED),
    loops: [
      {
        nodeId: "orders",
        instanceKey: "",
        count: 4,
        completed: 2,
        running: 1,
        nextIndex: 3,
        status: "failed",
      },
    ],
  } as ExecuteResponse;

  it("lists each item with the steps it went through, the failed one, and the items that didn't run", () => {
    expect(loopLog(failed, inOrders)).toEqual([
      {
        id: "orders",
        within: [],
        summary: "2 of 4 items finished",
        progress: 0.5,
        items: [
          {
            label: "Item 1",
            status: "completed",
            text: "check, route, send",
            step: "send",
          },
          {
            label: "Item 2",
            status: "completed",
            text: "check, route, send",
            step: "send",
          },
          {
            label: "Item 3",
            status: "failed",
            text: "Failed at reject",
            step: "reject",
          },
        ],
        notRun: "Item 4 didn't run.",
      },
    ]);
    expect(runStatus(failed, null)).toBe("Failed at reject, item 3");
  });

  it("says every item finished, and names nested items by the items around them", () => {
    const nested: TraceStep[] = [
      step("orders", "", "completed", "forEach"),
      step("lines", "lines[0]", "completed", "forEach"),
      step("reserve", "reserve[0][0]"),
      step("lines", "lines[1]", "completed", "forEach"),
      step("reserve", "reserve[1][0]"),
      step("reserve", "reserve[1][1]"),
    ];
    const response = {
      status: "completed",
      compile: { ok: true, sliced: false, diagnostics: [], stubs: [] },
      trace: traced(nested),
      loops: [
        {
          nodeId: "orders",
          instanceKey: "",
          count: 2,
          completed: 2,
          running: 0,
          nextIndex: 2,
          status: "completed",
        },
      ],
    } as ExecuteResponse;
    const log = loopLog(response, around);
    expect(log.map((loop) => [loop.id, loop.summary])).toEqual([
      ["orders", "2 of 2 items"],
      ["lines", "1 item"],
      ["lines", "2 items"],
    ]);
    expect(log[2].items.map((item) => item.label)).toEqual([
      "Item 2 › 1",
      "Item 2 › 2",
    ]);
    // The outer item names each step it went through once.
    expect(log[0].items[1].text).toBe("lines, reserve");
    // A failure in a nested loop: without the workflow, the trace says where.
    const failing = {
      ...response,
      status: "failed",
      trace: traced([
        ...nested.slice(0, 5),
        step("reserve", "reserve[1][1]", "failed"),
      ]),
    } as ExecuteResponse;
    expect(finalAnnouncement(failing)).toBe("orders failed at item 2.");
  });

  it("keeps each loop's items apart when loops run one after another", () => {
    const twoLoops: TraceStep[] = [
      step("orders", "", "completed", "forEach"),
      step("check", "check[0]"),
      step("returns", "", "completed", "forEach"),
      step("refund", "refund[0]"),
      step("refund", "refund[1]"),
    ];
    const log = loopLog(
      {
        status: "completed",
        compile: { ok: true, sliced: false, diagnostics: [], stubs: [] },
        trace: traced(twoLoops),
      } as ExecuteResponse,
      (id) => (id === "refund" ? ["returns"] : around(id)),
    );
    expect(log.map((loop) => [loop.id, loop.items.length])).toEqual([
      ["orders", 1],
      ["returns", 2],
    ]);
  });

  it("says the items that didn't run as a range", () => {
    const longer = {
      ...failed,
      loops: [{ ...failed.loops![0], count: 6 }],
    } as ExecuteResponse;
    expect(loopLog(longer, inOrders)[0].notRun).toBe(
      "Items 4 to 6 didn't run.",
    );
  });
});
