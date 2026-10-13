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
// The window's keys never act on the workflow while someone types: in a
// field, or in editable text (contenteditable in any form), as on the canvas.
import "@angular/compiler";
import { afterAll, describe, expect, it, vi } from "vitest";
vi.hoisted(() =>
  vi.stubGlobal("document", {
    addEventListener: vi.fn(),
    // The formula editor reads this when step details load.
    documentElement: { style: {} },
  }),
);
import { App } from "../src/app/app";
afterAll(() => vi.unstubAllGlobals());

function fixture() {
  const app = Object.create(App.prototype) as App;
  const undo = vi.fn();
  const redo = vi.fn();
  const runCommand = vi.fn();
  Object.assign(app, {
    view: "designer",
    editorNext: false,
    stepDetails: { ownedPublish: () => false },
    dialogs: { current: () => null },
    model: { selected: "" },
    api: { session: { profile: null } },
    undo,
    redo,
    runCommand,
  });
  return { app, undo, redo, runCommand };
}
const editable = (form: "rich" | "plain") => ({
  tagName: form === "rich" ? "P" : "DIV",
  isContentEditable: true,
  closest: (selector: string) =>
    selector.includes(".canvas") ? { className: "canvas" } : null,
});
const press = (key: string, target: unknown) => {
  const event = {
    key,
    ctrlKey: true,
    metaKey: false,
    shiftKey: false,
    target,
    preventDefault: vi.fn(),
  };
  return event as unknown as KeyboardEvent;
};

describe("the window's keys while typing in editable text", () => {
  for (const form of ["rich", "plain"] as const)
    it(`never undo or save from ${form} editable text`, () => {
      const { app, undo, redo, runCommand } = fixture();
      app.key(press("z", editable(form)));
      app.key(press("s", editable(form)));
      expect(undo).not.toHaveBeenCalled();
      expect(redo).not.toHaveBeenCalled();
      expect(runCommand).not.toHaveBeenCalled();
    });

  it("still undo from the canvas itself", () => {
    const { app, undo } = fixture();
    app.key(
      press("z", {
        tagName: "DIV",
        isContentEditable: false,
        closest: (selector: string) =>
          selector.includes(".canvas") && !selector.includes("input")
            ? { className: "canvas" }
            : null,
      }),
    );
    expect(undo).toHaveBeenCalledTimes(1);
  });
});
