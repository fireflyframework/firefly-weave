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
