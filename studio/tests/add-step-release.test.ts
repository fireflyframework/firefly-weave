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
// Add a step holds the keys pressed while it opens; closing it, or failing
// to load it, lets go of them.
import "@angular/compiler";
import { afterAll, describe, expect, it, vi } from "vitest";
const listeners = vi.hoisted(() => {
  const added = new Set<unknown>();
  vi.stubGlobal("document", {
    addEventListener: (_type: string, listener: unknown) =>
      void added.add(listener),
    removeEventListener: (_type: string, listener: unknown) =>
      void added.delete(listener),
  });
  return added;
});
import { App } from "../src/app/app";
import {
  PANEL_FAILED,
  panelFailed,
} from "../src/app/editor/add-step/add-step-failed";
import { EarlyKeys } from "../src/app/editor/add-step/early-keys";
afterAll(() => vi.unstubAllGlobals());

/** The shell's Add a step state, holding keys as an opening panel does. */
function opening() {
  const said: string[] = [];
  const shell = Object.create(App.prototype) as App;
  const earlyKeys = new EarlyKeys("weave-add-step-panel");
  Object.assign(shell, {
    earlyKeys,
    picker: null,
    addStep: { insert: { owner: "root", index: 0 } },
    cdr: { markForCheck: () => undefined },
    notify: (line: string) => void said.push(line),
  });
  const before = listeners.size;
  earlyKeys.start();
  return { shell, said, before };
}

describe("letting go of the keys Add a step held", () => {
  it("closing the panel before it is drawn stops holding keys", () => {
    const { shell, before } = opening();
    expect(listeners.size).toBe(before + 1);
    shell.closePicker();
    expect(shell.addStep).toBeNull();
    expect(listeners.size).toBe(before);
  });

  it("when the panel's code can't load, closes it, lets go of the keys and says why", () => {
    const { shell, said, before } = opening();
    panelFailed(shell);
    expect(shell.addStep).toBeNull();
    expect(listeners.size).toBe(before);
    expect(said).toEqual([PANEL_FAILED]);
  });
});
