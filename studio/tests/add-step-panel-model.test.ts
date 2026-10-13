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
import { buildCatalog } from "../src/app/editor/add-step/entries";
import {
  reduce,
  shown,
  startState,
  type PanelAction,
  type PanelState,
} from "../src/app/editor/add-step/panel-model";
import { catalogInput } from "./add-step-fixtures";

const catalog = buildCatalog(catalogInput());
const press = (state: PanelState, ...actions: PanelAction[]) =>
  actions.reduce(
    (current, action) => reduce(current, action, catalog, false).state,
    state,
  );
const view = (state: PanelState) => shown(state, catalog, false);
const active = (state: PanelState) => view(state).options[state.active];
const down = { type: "move", delta: 1 } as const;

describe("moving through Add a step", () => {
  it("walks into Action in an app › HTTP APIs and back, keeping the place", () => {
    let state = press(startState("next"), down);
    expect(active(state).label).toBe("Action in an app");
    state = press(state, { type: "open" });
    expect(view(state).view.title).toBe("Action in an app");
    expect(active(state).label).toBe("Call an API");
    state = press(state, down, down, { type: "open" });
    expect(view(state).view.title).toBe("HTTP APIs");
    expect(view(state).parent?.title).toBe("Action in an app");
    state = press(state, { type: "back" });
    expect(view(state).view.title).toBe("Action in an app");
    expect(active(state).label).toBe("HTTP APIs");
    state = press(state, { type: "back" });
    expect(view(state).view.title).toBe("What happens next?");
    expect(active(state).label).toBe("Action in an app");
    expect(reduce(state, { type: "back" }, catalog, false).handled).toBe(false);
  });

  it("opens a category with Enter and chooses an entry; a disabled entry does nothing", () => {
    let state = press(startState("next"), down, down, down);
    state = press(state, { type: "enter" });
    expect(view(state).view.title).toBe("Flow");
    expect(reduce(state, { type: "enter" }, catalog, false).effect).toEqual({
      kind: "choose",
      entry: active(state),
    });
    state = press(state, { type: "hover", index: 2 });
    expect(active(state).label).toBe("Loop over items");
    expect(reduce(state, { type: "enter" }, catalog, false)).toMatchObject({
      effect: { kind: "none" },
      handled: true,
    });
  });

  it("wraps around with the arrows and starts each search at the top", () => {
    let state = press(startState("next"), { type: "move", delta: -1 });
    expect(active(state).label).toBe("Add another trigger");
    state = press(state, { type: "type", query: "wait" });
    expect(state.active).toBe(0);
    expect(view(state).searching).toBe(true);
    expect(active(state).label).toBe("Wait for time");
  });

  it("leaves → on an entry without a view, and ← at the start, to the search field", () => {
    const flow = press(startState("next"), down, down, down, { type: "open" });
    expect(reduce(flow, { type: "open" }, catalog, false).handled).toBe(false);
    expect(
      reduce(startState("triggers"), { type: "back" }, catalog, false).handled,
    ).toBe(false);
  });

  it("counts where each section starts", () => {
    const state = startState("triggers");
    const triggers = shown(
      state,
      buildCatalog(catalogInput({ hasSteps: false })),
      true,
    );
    expect(triggers.starts).toEqual([0, 7]);
    expect(triggers.options).toHaveLength(8);
  });
});
