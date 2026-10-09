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
  NO_SELECTION,
  blockedReason,
  contiguity,
  covered,
  extended,
  moveAllowed,
  only,
  selectionOf,
  toggled,
  toggledCovering,
  topLevel,
  type Selection,
  type StepPlace,
} from "../src/app/editor/state/selection";

const place = (
  id: string,
  owner: string,
  index: number,
  parent: string | null,
  order: number,
): [string, StepPlace] => [id, { id, owner, index, parent, order }];
// load, check, route (approve, notify | reject), finish
const places = new Map<string, StepPlace>([
  place("load", "root", 0, null, 1),
  place("check", "root", 1, null, 2),
  place("route", "root", 2, null, 3),
  place("approve", "route/case 1", 0, "route", 4),
  place("notify", "route/case 1", 1, "route", 5),
  place("reject", "route/default", 0, "route", 6),
  place("finish", "root", 3, null, 7),
]);

describe("the canvas selection", () => {
  it("selects one step, toggles others in and out, and keeps the focus on the last one added", () => {
    expect(only("check")).toEqual({ ids: ["check"], focus: "check" });
    const two = toggled(only("check"), "finish");
    expect(two).toEqual({ ids: ["check", "finish"], focus: "finish" });
    expect(toggled(two, "finish")).toEqual({ ids: ["check"], focus: "check" });
    expect(toggled(only("check"), "check")).toEqual(NO_SELECTION);
    expect(selectionOf(["load", "load", "check"], "check")).toEqual({
      ids: ["load", "check"],
      focus: "check",
    });
  });

  it("toggles a step a selected group covers out of it, and never lists a group with a step inside it", () => {
    // route's first path also holds fan, a parallel step with two paths.
    const nested = new Map<string, StepPlace>([
      ...places,
      place("fan", "route/case 1", 2, "route", 5.5),
      place("left", "fan/a", 0, "fan", 5.6),
      place("right", "fan/b", 0, "fan", 5.7),
    ]);
    const coherent = (selection: Selection) =>
      expect(topLevel(selection, nested)).toHaveLength(selection.ids.length);
    // Taken out of a selected decision: the decision gives way to the
    // steps still selected inside it, a group among them whole.
    const out = toggledCovering(only("route"), "approve", nested);
    expect(out).toEqual({ ids: ["notify", "fan", "reject"], focus: "reject" });
    coherent(out);
    // Two levels down: both groups above give way.
    const deep = toggledCovering(
      selectionOf(["load", "route"], "load"),
      "left",
      nested,
    );
    expect(deep).toEqual({
      ids: ["load", "approve", "notify", "right", "reject"],
      focus: "load",
    });
    coherent(deep);
    // Clicked again, it comes back on its own.
    expect(toggledCovering(out, "approve", nested)).toEqual({
      ids: ["notify", "fan", "reject", "approve"],
      focus: "approve",
    });
    // A group comes in place of the selected steps inside it.
    const group = toggledCovering(
      selectionOf(["load", "approve", "left"]),
      "route",
      nested,
    );
    expect(group).toEqual({ ids: ["load", "route"], focus: "route" });
    coherent(group);
    // Steps no group covers toggle as before.
    expect(toggledCovering(only("check"), "finish", nested)).toEqual(
      toggled(only("check"), "finish"),
    );
    expect(
      toggledCovering(selectionOf(["check", "finish"]), "finish", nested),
    ).toEqual({ ids: ["check"], focus: "check" });
  });

  it("counts a group's steps as selected and reports only the top-level ones, in document order", () => {
    expect([...covered(only("route"), places)].sort()).toEqual([
      "approve",
      "notify",
      "reject",
      "route",
    ]);
    expect(topLevel(selectionOf(["notify", "route", "load"]), places)).toEqual([
      "load",
      "route",
    ]);
    expect(topLevel(selectionOf(["gone"]), places)).toEqual([]);
  });

  it("is one run when its top-level steps are consecutive in one sequence", () => {
    expect(contiguity(selectionOf(["check", "load"]), places)).toEqual({
      ok: true,
      owner: "root",
      from: 0,
      to: 1,
      ids: ["load", "check"],
    });
    expect(contiguity(selectionOf(["route", "approve"]), places)).toMatchObject(
      { ok: true, ids: ["route"] },
    );
    expect(contiguity(selectionOf(["load", "route"]), places)).toEqual({
      ok: false,
      reason: "spread",
    });
    expect(contiguity(selectionOf(["approve", "finish"]), places)).toEqual({
      ok: false,
      reason: "spread",
    });
    expect(contiguity(NO_SELECTION, places)).toEqual({
      ok: false,
      reason: "empty",
    });
  });

  it("says why a selection that isn't one run can't be copied, duplicated or extracted", () => {
    const spread = selectionOf(["load", "route"]);
    expect(blockedReason("copy", spread, places)).toBe(
      "Copy works on steps next to each other in one path. Select a single run of steps.",
    );
    expect(blockedReason("duplicate", spread, places)).toBe(
      "Duplicate works on steps next to each other in one path. Select a single run of steps.",
    );
    expect(blockedReason("extract", spread, places)).toBe(
      "Extract to sub-workflow works on steps next to each other in one path. Select a single run of steps.",
    );
    expect(blockedReason("move", NO_SELECTION, places)).toBe(
      "Select a step first.",
    );
    expect(
      blockedReason("cut", selectionOf(["load", "check"]), places),
    ).toBeNull();
  });

  it("extends the selection upstream and downstream within the focused step's sequence", () => {
    const once = extended(only("check"), places, "downstream");
    expect(once).toEqual({ ids: ["check", "route"], focus: "route" });
    expect(extended(once, places, "downstream")).toEqual({
      ids: ["check", "route", "finish"],
      focus: "finish",
    });
    expect(extended(only("load"), places, "upstream")).toEqual(only("load"));
    expect(extended(only("notify"), places, "upstream")).toEqual({
      ids: ["notify", "approve"],
      focus: "approve",
    });
  });

  it("allows a move that changes something and keeps a group out of its own lanes", () => {
    expect(moveAllowed(places, "check", { owner: "root", index: 1 })).toBe(
      false,
    );
    expect(moveAllowed(places, "check", { owner: "root", index: 2 })).toBe(
      false,
    );
    expect(moveAllowed(places, "check", { owner: "root", index: 0 })).toBe(
      true,
    );
    expect(
      moveAllowed(places, "route", { owner: "route/case 1", index: 0 }),
    ).toBe(false);
    expect(moveAllowed(places, "approve", { owner: "root", index: 0 })).toBe(
      true,
    );
    expect(
      moveAllowed(places, "load", { owner: "route/default", index: 1 }),
    ).toBe(true);
    expect(moveAllowed(places, "gone", { owner: "root", index: 0 })).toBe(
      false,
    );
  });
});
