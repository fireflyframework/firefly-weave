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
// The Platform event trigger in Studio's shell: an answer to its questions
// counts only while the workflow may still change, and removing it says what
// it waits for and asks once.
import "@angular/compiler";
import {
  afterAll,
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";
vi.hoisted(() =>
  vi.stubGlobal("document", {
    addEventListener: vi.fn(),
    // The formula editor reads this when step details load.
    documentElement: { style: {} },
  }),
);
import { App } from "../src/app/app";
import type { PanelEntry } from "../src/app/editor/add-step/entries";
import { placeMark } from "../src/app/editor/add-step/place";
import { REMOVAL_WAIT_MS } from "../src/app/editor/triggers/event-removal";
import { StructuredCanvasAdapter } from "../src/app/model";
import { ToastService } from "../src/app/toast";
afterAll(() => vi.unstubAllGlobals());

const FORM = {
  type: "object",
  required: ["reference"],
  properties: { reference: { type: "string" } },
};

/** Studio's shell around one workflow, with its questions answered by the test. */
function shell(inputSchema: unknown) {
  const app = Object.create(App.prototype) as App;
  const model = new StructuredCanvasAdapter();
  model.insert("wait");
  model.updateWorkflow({
    ...model.definition,
    spec: { ...model.definition.spec, inputSchema },
  });
  model.clearHistory();
  const asked: ((answer: boolean) => void)[] = [];
  const told: string[] = [];
  const toasts = new ToastService();
  Object.assign(app, {
    model,
    tab: "Canvas",
    view: "designer",
    sourceBuffer: model.source,
    cdr: { markForCheck() {} },
    changed() {},
    loadInspector() {},
    toasts,
    removals: new Set(),
    api: { session: {}, project: "", request: async () => ({}) },
    identity: null,
    dialogs: {
      confirm: () => new Promise<boolean>((answer) => asked.push(answer)),
    },
    notify(text: string) {
      told.push(text);
    },
    openChosenTrigger() {},
    insertOwner: () => () => true,
  });
  return { app, model, asked, told, toasts };
}
const settle = () => new Promise((done) => setTimeout(done, 20));
const kinds = (model: StructuredCanvasAdapter) =>
  (model.canvas.triggers ?? []).map((intent) => intent.kind);

describe("adding Platform event while its question is open", () => {
  it("adds nothing once the workflow's runs are open: it can't change there", async () => {
    const { app, model, asked, told } = shell(FORM);
    const at = { owner: "root", index: 0 };
    const adding = app.insertFromPanel(
      { insert: { kind: "trigger", trigger: "event" } } as PanelEntry,
      { at, opened: model.opened, mark: placeMark(model, at) } as never,
    );
    await settle();
    expect(asked).toHaveLength(1);
    app.tab = "Runs";
    asked[0]!(true);
    expect(await adding).toBe("gone");
    expect(told).toEqual([
      "That place in the workflow changed. Choose + again.",
    ]);
    expect("eventTrigger" in model.definition.spec).toBe(false);
    expect(model.definition.spec["inputSchema"]).toEqual(FORM);
  });
});

describe("removing Platform event while the platform answers", () => {
  beforeEach(() => void vi.useFakeTimers());
  afterEach(() => void vi.useRealTimers());

  /** A trigger an activation created, on a platform that doesn't answer. */
  function created() {
    const f = shell({ $ref: "weave.platform-event-v1" });
    f.model.updateWorkflow({
      ...f.model.definition,
      spec: {
        ...f.model.definition.spec,
        eventTrigger: { events: ["transaction.in_doubt"] },
      },
    });
    f.model.canvas = {
      ...f.model.canvas,
      triggers: [
        { id: "trigger-0000000e", kind: "event", resources: { dev: "et-1" } },
      ],
    };
    f.app.api.request = (() => new Promise(() => {})) as never;
    return f;
  }

  it("says what it waits for, until it asks", async () => {
    const { app, asked, toasts } = created();
    void app.removeTrigger("trigger-0000000e");
    await vi.advanceTimersByTimeAsync(REMOVAL_WAIT_MS - 100);
    expect(toasts.current()?.text).toBe("Checking which triggers still react…");
    expect(asked).toHaveLength(0);
    await vi.advanceTimersByTimeAsync(100);
    expect(asked).toHaveLength(1);
    expect(toasts.current()).toBeNull();
  });

  it("asks nothing once another workflow is open", async () => {
    const { app, model, asked } = created();
    void app.removeTrigger("trigger-0000000e");
    await vi.advanceTimersByTimeAsync(100);
    app.model = new StructuredCanvasAdapter();
    await vi.advanceTimersByTimeAsync(REMOVAL_WAIT_MS);
    expect(asked).toHaveLength(0);
    expect(kinds(model)).toEqual(["event"]);
  });

  it("asks one question however often Delete is pressed, and another after that one's answer", async () => {
    const { app, model, asked } = created();
    void app.removeTrigger("trigger-0000000e");
    await vi.advanceTimersByTimeAsync(100);
    void app.removeTrigger("trigger-0000000e");
    await vi.advanceTimersByTimeAsync(REMOVAL_WAIT_MS * 2);
    void app.removeTrigger("trigger-0000000e");
    await vi.advanceTimersByTimeAsync(REMOVAL_WAIT_MS * 2);
    expect(asked).toHaveLength(1);
    asked[0]!(false);
    await vi.advanceTimersByTimeAsync(0);
    expect(kinds(model)).toEqual(["event"]);
    void app.removeTrigger("trigger-0000000e");
    await vi.advanceTimersByTimeAsync(REMOVAL_WAIT_MS);
    expect(asked).toHaveLength(2);
    asked[1]!(true);
    await vi.advanceTimersByTimeAsync(0);
    expect(kinds(model)).toEqual([]);
    expect("eventTrigger" in model.definition.spec).toBe(false);
  });
});
