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
  fixtureIds,
  runViewsFixture,
} from "../src/app/operate/run-views.fixture";
import {
  instanceLabel,
  stepBar,
  timelineGroups,
  timelineSpan,
} from "../src/app/operate/runs/step-timeline";

const failed = runViewsFixture.steps[fixtureIds.failed].items;
const running = runViewsFixture.steps[fixtureIds.running].items;
const legacy = runViewsFixture.steps[fixtureIds.succeeded].items;

describe("Step timeline", () => {
  it("groups loop iterations under their step, in the order steps first appear", () => {
    const groups = timelineGroups(failed);
    expect(groups.map((group) => group.node_id)).toEqual([
      "fetch",
      "notify",
      "items",
      "send",
    ]);
    expect(groups[3].items.map((item) => item.instance_key)).toEqual([
      "send[0]",
      "send[1]",
      "send[2]",
      "send[3]",
    ]);
    // Instance keys are used as given: an agent's turns stay under their step.
    expect(
      timelineGroups(running).map((group) => [
        group.node_id,
        group.items.map((item) => item.instance_key),
      ]),
    ).toEqual([["support", ["", "support#1", "support#2"]]]);
    expect(timelineGroups([])).toEqual([]);
  });

  it("names an instance by its iteration indexes, or its instance key", () => {
    const [send0] = failed.filter((item) => item.instance_key === "send[0]");
    expect(instanceLabel(send0)).toBe("Iteration 0");
    expect(instanceLabel({ ...send0, iteration: [2, 1] })).toBe(
      "Iteration 2 › 1",
    );
    expect(instanceLabel(running[1])).toBe("support#1");
    expect(instanceLabel(failed[0])).toBe("");
  });

  it("draws queue wait and execution within the run's span", () => {
    const span = timelineSpan(failed, new Date("2026-10-07T10:05:00Z"));
    expect(span).toEqual({
      start: Date.parse("2026-10-07T10:00:01Z"),
      end: Date.parse("2026-10-07T10:01:05Z"),
    });
    // fetch: scheduled 10:00:01, started 10:00:02, ended 10:00:05.
    expect(stepBar(failed[0], span!, new Date())).toEqual({
      offset: 0,
      wait: (1 / 64) * 100,
      run: (3 / 64) * 100,
      open: false,
    });
    // A step still running ends at the server's now, and says so.
    const live = timelineSpan(running, new Date("2026-10-07T11:30:09Z"))!;
    expect(live).toEqual({
      start: Date.parse("2026-10-07T11:30:01Z"),
      end: Date.parse("2026-10-07T11:30:09Z"),
    });
    expect(stepBar(running[2], live, new Date("2026-10-07T11:30:09Z"))).toEqual(
      {
        offset: (3 / 8) * 100,
        wait: 0,
        run: (5 / 8) * 100,
        open: true,
      },
    );
  });

  it("draws nothing for steps recorded without timings", () => {
    expect(timelineSpan(legacy, new Date())).toBeNull();
    const span = timelineSpan(failed, new Date())!;
    expect(stepBar(legacy[0], span, new Date())).toBeNull();
  });
});
