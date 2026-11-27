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
  edgeRun,
  frameRun,
  framedStep,
  groupsEntered,
  pulses,
  tileRun,
  type CanvasRun,
} from "../src/app/editor/canvas/run-state";

const run = (extra: Partial<CanvasRun> = {}): CanvasRun => ({
  mode: "simulated",
  status: "running",
  current: [],
  active: [],
  done: [],
  ...extra,
});

describe("a step's run state", () => {
  it("is nothing without a run", () => {
    expect(tileRun("a", null)).toBeNull();
  });

  it("is live where the run is, waiting where it waits, and done once finished", () => {
    expect(tileRun("b", run({ current: ["b"] }))).toBe("live");
    expect(
      tileRun("b", run({ status: "waiting", current: ["b"], active: ["b"] })),
    ).toBe("waiting");
    expect(tileRun("a", run({ done: ["a"] }))).toBe("done");
    expect(tileRun("c", run({ done: ["a"] }))).toBeNull();
  });

  it("is failed where a failed run stopped", () => {
    expect(tileRun("b", run({ status: "failed", current: ["b"] }))).toBe(
      "failed",
    );
    expect(tileRun("b", run({ status: "timed_out", active: ["b"] }))).toBe(
      "failed",
    );
    expect(
      tileRun("b", run({ status: "cancelled", current: ["b"] })),
    ).toBeNull();
  });

  it("keeps a finished step's check once the run ended, even if the run still lists it", () => {
    expect(
      tileRun(
        "b",
        run({
          status: "succeeded",
          current: ["b"],
          active: ["b"],
          done: ["b"],
        }),
      ),
    ).toBe("done");
    expect(
      tileRun("b", run({ status: "cancelled", active: ["b"], done: ["b"] })),
    ).toBe("done");
    // Still going, a step both listed and finished is where the run is.
    expect(tileRun("b", run({ current: ["b"], done: ["b"] }))).toBe("live");
    // A failed run marks the step it failed at, finished or not.
    expect(
      tileRun("b", run({ status: "failed", current: ["b"], done: ["b"] })),
    ).toBe("failed");
  });
});

describe("an edge's run state", () => {
  const ab = { leaves: "a", enters: "b" };

  it("is idle without a run, and for an edge that names no step", () => {
    expect(edgeRun(ab, null)).toBe("idle");
    expect(
      edgeRun(
        { leaves: null, enters: null },
        run({ status: "succeeded", done: ["a"] }),
      ),
    ).toBe("idle");
  });

  it("is live while the run takes it, taken once the run ended, and skipped when the run went elsewhere", () => {
    expect(edgeRun(ab, run({ done: ["a"], current: ["b"] }))).toBe("live");
    expect(edgeRun(ab, run({ status: "succeeded", done: ["a", "b"] }))).toBe(
      "taken",
    );
    expect(edgeRun(ab, run({ status: "succeeded", done: ["a", "c"] }))).toBe(
      "skipped",
    );
    expect(edgeRun(ab, run({ done: ["a"] }))).toBe("idle");
    expect(
      edgeRun(
        { leaves: null, enters: "b" },
        run({ status: "succeeded", done: ["b"] }),
      ),
    ).toBe("taken");
    expect(
      edgeRun(
        { leaves: "z", enters: "$end" },
        run({ status: "succeeded", done: ["z"] }),
      ),
    ).toBe("taken");
    expect(
      edgeRun(
        { leaves: "z", enters: "$end" },
        run({ status: "failed", done: ["z"] }),
      ),
    ).toBe("skipped");
  });

  it("pulses only while a real run is followed, never during a simulated one", () => {
    expect(pulses("live", run({ mode: "real" }))).toBe(true);
    expect(pulses("waiting", run({ mode: "real" }))).toBe(true);
    expect(pulses("live", run({ mode: "simulated" }))).toBe(false);
    expect(pulses("done", run({ mode: "real" }))).toBe(false);
    expect(pulses("live", null)).toBe(false);
  });
});

describe("the groups a run is inside", () => {
  // route holds review and the nested group inner (which holds deep).
  const holder: Record<string, string> = {
    review: "route",
    inner: "route",
    deep: "inner",
  };
  const parentOf = (id: string) => holder[id] ?? null;

  it("is every group that holds a step the run is at or finished, however deep", () => {
    expect(groupsEntered(null, parentOf).size).toBe(0);
    expect(groupsEntered(run({ done: ["check"] }), parentOf).size).toBe(0);
    expect([
      ...groupsEntered(run({ active: ["review"], done: ["check"] }), parentOf),
    ]).toEqual(["route"]);
    expect(
      [...groupsEntered(run({ current: ["deep"] }), parentOf)].sort(),
    ).toEqual(["inner", "route"]);
    expect([
      ...groupsEntered(
        run({ status: "failed", done: ["review", "check"] }),
        parentOf,
      ),
    ]).toEqual(["route"]);
  });

  it("makes the edge into a group taken or live as soon as the run is inside it, though the run records the group only when its join completes", () => {
    const into = { leaves: "check", enters: "route" };
    const fromTrigger = { leaves: null, enters: "route" };
    const waiting = run({
      status: "waiting",
      current: ["review"],
      active: ["review"],
      done: ["check"],
    });
    const inside = groupsEntered(waiting, parentOf);
    expect(edgeRun(into, waiting)).toBe("idle");
    expect(edgeRun(into, waiting, inside)).toBe("live");
    expect(edgeRun(fromTrigger, waiting, inside)).toBe("live");
    for (const status of ["failed", "cancelled", "timed_out"]) {
      const ended = run({ ...waiting, status });
      expect(edgeRun(into, ended)).toBe("skipped");
      expect(edgeRun(into, ended, groupsEntered(ended, parentOf))).toBe(
        "taken",
      );
    }
  });

  it("leaves the edges that leave a group to the group's own finish", () => {
    const leavingNested = { leaves: "inner", enters: null };
    const waiting = run({ status: "waiting", active: ["deep"] });
    const inside = groupsEntered(waiting, parentOf);
    // Inside the nested group is not out of it: this edge waits for its join.
    expect(edgeRun(leavingNested, waiting, inside)).toBe("idle");
    expect(
      edgeRun(
        leavingNested,
        run({ status: "failed", active: ["deep"] }),
        groupsEntered(run({ status: "failed", active: ["deep"] }), parentOf),
      ),
    ).toBe("skipped");
    expect(
      edgeRun(
        leavingNested,
        run({ status: "succeeded", done: ["deep", "inner"] }),
        groupsEntered(run({ done: ["deep", "inner"] }), parentOf),
      ),
    ).toBe("taken");
  });
});

describe("how the Runs tab frames a run", () => {
  const workflow = { x: 0, y: 0, width: 1000, height: 200 };
  const send = { x: 800, y: 0, width: 120, height: 100 };

  it("fits the workflow at the largest zoom from 100% to 60%, centered", () => {
    // 1000 px wide with 8 px on each side fits a 896 px area at 88%.
    expect(frameRun(workflow, send, { width: 896, height: 700 })).toEqual({
      zoom: 0.88,
      pan: { x: 8, y: 262 },
    });
    // Room to spare: never above 100%.
    expect(frameRun(workflow, send, { width: 2000, height: 700 }).zoom).toBe(1);
  });

  it("centers one step at 60% when the workflow doesn't fit, its outline filling the width", () => {
    // At 60% the workflow is 600 px wide: more than a 424 px column. Centered
    // on send (pan -304), its right edge would sit 128 px inside the column;
    // the pan stops where that edge meets the column's (424 - 600 = -176).
    // Down, the 120 px outline fits the 900 px column: it is centered.
    const framed = frameRun(workflow, send, { width: 424, height: 900 });
    expect(framed).toEqual({ zoom: 0.6, pan: { x: -176, y: 390 } });
    // send stays in view: 800 × 0.6 - 176 = 304, its right side at 376.
    expect(800 * 0.6 + framed.pan.x).toBeGreaterThanOrEqual(0);
    expect(920 * 0.6 + framed.pan.x).toBeLessThanOrEqual(424);
  });

  it("centers a step in the middle of a long workflow, both outline edges outside", () => {
    const long = { x: 0, y: 0, width: 3000, height: 200 };
    const middle = { x: 1440, y: 0, width: 120, height: 100 };
    // (424 - 72) / 2 - 1440 × 0.6 = -688: the outline spans -688 to 1112.
    expect(frameRun(long, middle, { width: 424, height: 900 }).pan.x).toBe(
      -688,
    );
  });

  it("frames the whole run between 55% and 60% on a wide canvas before cropping it", () => {
    // 1,400 wide: at 56%, 784 + 16 spare fits an 800 px canvas; at 57% it doesn't.
    const wide = { x: 0, y: 0, width: 1400, height: 200 };
    const framed = frameRun(wide, send, { width: 800, height: 700 });
    expect(framed.zoom).toBe(0.56);
    expect(framed.pan.x).toBeCloseTo(8);
  });

  describe("a step inside a transaction, on a phone", () => {
    const long = { x: 0, y: 0, width: 1600, height: 200 };
    // 967 wide: 580 px at 60%, wider than a 360 px phone.
    const move = { x: 300, y: 0, width: 967, height: 160 };
    const submit = { x: 1100, y: 40, width: 120, height: 100 };
    const phone = { width: 360, height: 700 };

    it("opens on the transaction's right end, where its header says how it stands", () => {
      const framed = frameRun(long, submit, phone, move);
      expect(framed.zoom).toBe(0.6);
      // The frame ends 16 px inside the canvas: 1,267 × 0.6 - 416.2 = 344.
      expect((move.x + move.width) * 0.6 + framed.pan.x).toBeCloseTo(344);
      // The step stays in view.
      expect(submit.x * 0.6 + framed.pan.x).toBeGreaterThanOrEqual(8);
      // Down, as before: the outline is centered.
      expect(framed.pan.y).toBe(frameRun(long, submit, phone).pan.y);
    });

    it("shows the transaction's end even when its step is further left", () => {
      // How the transaction stands comes first on a phone; the run's header names the step.
      const hold = { x: 320, y: 40, width: 120, height: 100 };
      const framed = frameRun(long, hold, phone, move);
      expect((move.x + move.width) * 0.6 + framed.pan.x).toBeCloseTo(344);
      expect((hold.x + hold.width) * 0.6 + framed.pan.x).toBeLessThan(0);
    });

    it("leaves no room empty before the workflow's first step", () => {
      // A transaction that starts the workflow: centered, 30 px before it would stay empty.
      const first = { x: 0, y: 0, width: 500, height: 160 };
      expect(frameRun(long, submit, phone, first).pan.x).toBeCloseTo(0);
    });

    it("centers a transaction that fits the phone's width", () => {
      const narrow = { x: 300, y: 0, width: 500, height: 160 };
      const framed = frameRun(long, submit, phone, narrow);
      // 300 px at 60%: 30 px on each side.
      expect(narrow.x * 0.6 + framed.pan.x).toBeCloseTo(30);
    });

    it("frames a wide canvas as before", () => {
      const wide = { width: 900, height: 700 };
      expect(frameRun(long, submit, wide, move)).toEqual(
        frameRun(long, submit, wide),
      );
    });
  });

  it("keeps 60% and the framed step below 768 px, where 55% would read too small", () => {
    // 680 wide would fit a 400 px column at 56%, but a phone keeps 60%.
    const short = { x: 0, y: 0, width: 680, height: 200 };
    expect(frameRun(short, send, { width: 400, height: 700 }).zoom).toBe(0.6);
  });

  it("measures the outline at each zoom it tries", () => {
    // Labels grow as the canvas zooms out: the outline widens with them.
    const outline = (zoom: number) => ({
      x: 0,
      y: 0,
      width: 1000 + (1 - zoom) * 200,
      height: 200,
    });
    // 86%: 1,028 × 0.86 = 884.08 + 16 fits 901; 88%: 1,024 × 0.88 = 901.12 doesn't.
    const framed = frameRun(outline, send, { width: 901, height: 700 });
    expect(framed.zoom).toBe(0.86);
    expect(framed.pan.x).toBeCloseTo((901 - 1028 * 0.86) / 2);
  });

  it("frames the failed step, else the running one, else the last finished", () => {
    const tiles = ["$trigger:manual", "fetch", "check", "send"];
    const recorded = (extra: Partial<CanvasRun>) => ({
      ...run({ mode: "real", ...extra }),
      changed: [],
      added: [],
      failures: {},
    });
    expect(
      framedStep(
        recorded({ status: "failed", current: ["send"], done: ["fetch"] }),
        tiles,
      ),
    ).toBe("send");
    expect(
      framedStep(
        recorded({ status: "running", current: ["check"], done: ["fetch"] }),
        tiles,
      ),
    ).toBe("check");
    expect(
      framedStep(
        recorded({ status: "succeeded", done: ["send", "fetch", "check"] }),
        tiles,
      ),
    ).toBe("send");
    expect(framedStep(recorded({ status: "queued" }), tiles)).toBe(
      "$trigger:manual",
    );
  });
});

describe("the edges of a run that happened", () => {
  it("dashes only the ways a finished step didn't take; edges past a failure stay plain", () => {
    const failed = run({
      mode: "real",
      status: "failed",
      current: ["send"],
      done: ["fetch", "route"],
    });
    // The way route didn't take: skipped.
    expect(edgeRun({ leaves: "route", enters: "review" }, failed)).toBe(
      "skipped",
    );
    // Out of the failed step, and on from a step the run never reached: idle.
    expect(edgeRun({ leaves: "send", enters: "$end" }, failed)).toBe("idle");
    expect(edgeRun({ leaves: "review", enters: "$end" }, failed)).toBe("idle");
    // A simulation keeps its whole untaken path dashed.
    expect(
      edgeRun(
        { leaves: "review", enters: "$end" },
        { ...failed, mode: "simulated" },
      ),
    ).toBe("skipped");
  });
});

describe("a step new in the draft, on a run of an older version", () => {
  it("dashes both edges around it: the run went past it, not through it", () => {
    const older = {
      ...run({
        mode: "real",
        status: "succeeded",
        done: ["fetch", "approve", "send"],
      }),
      changed: [],
      added: ["check"],
      failures: {},
    };
    expect(edgeRun({ leaves: "fetch", enters: "check" }, older)).toBe(
      "skipped",
    );
    expect(edgeRun({ leaves: "check", enters: "approve" }, older)).toBe(
      "skipped",
    );
  });
});
