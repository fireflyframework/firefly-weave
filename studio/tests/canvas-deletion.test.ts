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
// Deleting steps from the canvas: one rule for one step and several (a
// step's error path goes with it unasked; a group with steps inside asks
// first), the toast's words with and without pinned output, and the
// workflow read once, before the wait for those words' code.
import "@angular/compiler";
import { afterAll, describe, expect, it, vi } from "vitest";
vi.hoisted(() =>
  vi.stubGlobal("document", {
    addEventListener: vi.fn(),
    documentElement: { style: {} },
  }),
);
import { App } from "../src/app/app";
import { deletionOf } from "../src/app/editor/canvas/deletion";
import {
  StructuredCanvasAdapter,
  freshWorkflow,
  type Step,
  type Workflow,
} from "../src/app/model";
import type { ToastAction } from "../src/app/toast";
afterAll(() => vi.unstubAllGlobals());

const transform = (id: string): Step => ({
  id,
  kind: "transform",
  value: { literal: 1 },
});
/** submit, whose error path holds notify-team and flag-request; a decision route with one step inside; after. */
function workflow(): Workflow {
  const document = freshWorkflow();
  document.spec.steps = [
    {
      id: "submit",
      kind: "transform",
      value: { literal: 1 },
      onError: "errorOutput",
      errorPath: {
        steps: [transform("notify-team"), transform("flag-request")],
        output: { literal: null },
      },
    },
    {
      id: "route",
      kind: "switch",
      cases: [
        {
          when: { literal: true },
          steps: [transform("record")],
          output: { literal: null },
        },
      ],
      default: { steps: [], output: { literal: null } },
    },
    transform("after"),
  ];
  return document;
}
/** What the model counts inside each step: an error path's steps included. */
function counted(ids: string[]) {
  const model = new StructuredCanvasAdapter();
  model.setSource(JSON.stringify(workflow()), "json");
  expect(model.error).toBe("");
  return ids.map((id) => model.containedSteps(id));
}
const deleting = (ids: string[]) => deletionOf(workflow(), ids, counted(ids));

describe("deleting steps from the canvas", () => {
  it("asks nothing for a step with an error path, alone or with another step", () => {
    expect(deleting(["submit"]).question).toBeNull();
    expect(deleting(["submit", "after"]).question).toBeNull();
  });

  it("asks first for a group with steps inside, counting no error path's steps", () => {
    expect(deleting(["route"]).question?.title).toBe(
      "Delete route and the 1 step inside it?",
    );
    expect(deleting(["submit", "route"]).question?.title).toBe(
      "Delete 2 steps and the 1 step inside them?",
    );
  });

  it("names the error path that went, with the pinned output when it went too", () => {
    const one = deleting(["submit"]);
    expect(one.done(false)).toEqual({
      text: "Deleted submit and its error path (2 steps)",
      restored: "Restored submit and its error path.",
      detail: "notify-team and flag-request were in it.",
    });
    expect(one.done(true).text).toBe(
      "Deleted submit, its error path (2 steps) and its pinned output",
    );
    expect(one.done(true).restored).toBe(
      "Restored submit, its error path and its pinned output.",
    );
    const two = deleting(["submit", "after"]).done(true);
    expect(two.text).toBe(
      "Deleted 2 steps, an error path (2 steps) and their pinned output",
    );
    expect(two.detail).toBe("notify-team and flag-request were in it.");
  });

  it("says what went without an error path as it always has", () => {
    expect(deleting(["after"]).done(false)).toEqual({
      text: "Deleted after.",
      restored: "Restored after.",
      detail: undefined,
    });
    expect(deleting(["route", "after"]).done(true).text).toBe(
      "Deleted 2 steps and their pinned output.",
    );
  });
});

describe("a delete that waits for its words' code", () => {
  it("deletes nothing when the workflow changed during the wait", async () => {
    const app = Object.create(App.prototype) as App;
    const model = new StructuredCanvasAdapter();
    model.setSource(JSON.stringify(workflow()), "json");
    const toasts: string[] = [];
    Object.assign(app, {
      model,
      sourceBuffer: model.source,
      cdr: { markForCheck() {} },
      changed() {
        Object.assign(app, { sourceBuffer: model.source });
      },
      loadInspector() {},
      focusStep() {},
      testData: {
        data: () => ({}),
        prune: () => [],
        version: () => 0,
        restore: () => true,
      },
      notify(text: string, _action?: ToastAction) {
        toasts.push(text);
      },
    });
    const pending = (
      app as unknown as { deleteSteps(ids: string[]): Promise<boolean> }
    ).deleteSteps(["submit"]);
    // An edit lands while the words load: the deletion the person asked
    // for was of the workflow they saw, so nothing goes.
    model.insert("wait");
    expect(await pending).toBe(false);
    expect(model.stepIdSet().has("submit")).toBe(true);
    expect(toasts).toEqual([]);
  });
});
