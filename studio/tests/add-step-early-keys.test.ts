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
import { afterEach, describe, expect, it, vi } from "vitest";
import { EarlyKeys, heldKey } from "../src/app/editor/add-step/early-keys";

type Listener = (event: KeyboardEvent) => void;
const listeners = new Set<Listener>();
vi.stubGlobal("document", {
  addEventListener: (_type: string, listener: Listener) =>
    listeners.add(listener),
  removeEventListener: (_type: string, listener: Listener) =>
    listeners.delete(listener),
});
afterEach(() => listeners.clear());

/** A keydown as the page would dispatch it, on a target matching `matches`. */
function press(
  key: string,
  matches: string[] = [],
  modifiers: Partial<KeyboardEvent> = {},
) {
  const event = {
    key,
    code: key.length === 1 ? `Key${key.toUpperCase()}` : key,
    shiftKey: false,
    ctrlKey: false,
    metaKey: false,
    altKey: false,
    isComposing: false,
    target: {
      closest: (selector: string) => (matches.includes(selector) ? {} : null),
    },
    prevented: false,
    preventDefault() {
      this.prevented = true;
    },
    stopImmediatePropagation() {},
    ...modifiers,
  };
  for (const listener of listeners) listener(event as unknown as KeyboardEvent);
  return event.prevented;
}
const FIELDS =
  "input, textarea, select, [contenteditable]:not([contenteditable='false'])";

describe("keys pressed while Add a step opens", () => {
  it("holds typed characters and the panel's keys, in order, until taken", () => {
    const keys = new EarlyKeys("weave-add-step-panel");
    keys.start();
    expect(press("h")).toBe(true);
    expect(press("ArrowDown")).toBe(true);
    expect(press("Enter")).toBe(true);
    expect(keys.take().map((key) => key.key)).toEqual([
      "h",
      "ArrowDown",
      "Enter",
    ]);
    expect(press("t")).toBe(false);
    expect(keys.take()).toEqual([]);
  });

  it("lets shortcuts with a modifier, Tab and typing in another field through", () => {
    const keys = new EarlyKeys("weave-add-step-panel");
    keys.start();
    expect(press("z", [], { metaKey: true })).toBe(false);
    expect(press("Tab")).toBe(false);
    expect(press("a", [FIELDS])).toBe(false);
    expect(press("a", [FIELDS, "weave-add-step-panel"])).toBe(true);
    expect(keys.take().map((key) => key.key)).toEqual(["a"]);
  });

  it("forgets what it held when stopped or started again", () => {
    const keys = new EarlyKeys("weave-add-step-panel");
    keys.start();
    press("x");
    keys.start();
    press("y");
    expect(keys.take().map((key) => key.key)).toEqual(["y"]);
    keys.start();
    press("z");
    keys.stop();
    expect(keys.take()).toEqual([]);
    expect(listeners.size).toBe(0);
  });

  it("reads which keys it holds", () => {
    const plain = {
      ctrlKey: false,
      metaKey: false,
      altKey: false,
      isComposing: false,
    };
    expect(heldKey({ ...plain, key: "/" })).toBe(true);
    expect(heldKey({ ...plain, key: "Escape" })).toBe(true);
    expect(heldKey({ ...plain, key: "Shift" })).toBe(false);
    expect(heldKey({ ...plain, key: "a", isComposing: true })).toBe(false);
  });
});
