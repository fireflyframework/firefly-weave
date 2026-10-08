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
import "@angular/compiler";
import { describe, expect, it } from "vitest";
import {
  coveredBy,
  keepUsable,
  sheetWhen,
  wrapTab,
  type TreeNode,
} from "../src/app/modal-sheet";

/** A tiny element tree: name(children...). */
interface Fake extends TreeNode<Fake> {
  name: string;
  parentElement: Fake | null;
  children: Fake[];
}
function tree(name: string, ...children: Fake[]): Fake {
  const node: Fake = { name, parentElement: null, children };
  for (const child of children) child.parentElement = node;
  return node;
}
const find = (node: Fake, name: string): Fake | undefined =>
  node.name === name
    ? node
    : node.children.map((c) => find(c, name)).find(Boolean);

describe("modal sheet: what it covers", () => {
  // body > app > shell > [sidebar, main > [topbar, toolbar, live, dialogs,
  //   grid > [palette, canvas, inspector]]]
  const body = tree(
    "body",
    tree(
      "app",
      tree(
        "shell",
        tree("sidebar"),
        tree(
          "main",
          tree("topbar"),
          tree("toolbar"),
          tree("live"),
          tree("dialogs"),
          tree("grid", tree("palette"), tree("canvas"), tree("inspector")),
        ),
      ),
    ),
    tree("scrim"),
  );
  const keep = (node: Fake) => ["live", "dialogs", "scrim"].includes(node.name);

  it("covers every sibling on the way up, and nothing inside the sheet", () => {
    const covered = coveredBy(find(body, "inspector")!, body, keep).map(
      (n) => n.name,
    );
    expect(covered.sort()).toEqual(
      ["canvas", "palette", "sidebar", "toolbar", "topbar"].sort(),
    );
  });

  it("leaves live regions and dialogs usable", () => {
    const covered = coveredBy(find(body, "palette")!, body, keep).map(
      (n) => n.name,
    );
    expect(covered).not.toContain("live");
    expect(covered).not.toContain("dialogs");
    expect(covered).not.toContain("scrim");
    expect(covered).toContain("inspector");
  });

  it("stops at the root it is given", () => {
    const main = find(body, "main")!;
    const covered = coveredBy(find(body, "canvas")!, main, keep).map(
      (n) => n.name,
    );
    expect(covered.sort()).toEqual(
      ["inspector", "palette", "toolbar", "topbar"].sort(),
    );
  });

  it("keeps dialogs, pickers and live regions out of the inert set", () => {
    for (const selector of [
      "[aria-live]",
      '[role="status"]',
      '[role="alert"]',
      "weave-modal",
      "weave-dialog-host",
      "weave-step-picker",
      "weave-resolve-incident-dialog",
    ])
      expect(keepUsable).toContain(selector);
  });
});

describe("modal sheet: Tab stays inside", () => {
  it("wraps from the last control to the first, and back", () => {
    expect(wrapTab(3, 4, false)).toBe(0);
    expect(wrapTab(0, 4, true)).toBe(3);
  });

  it("lets the browser move between controls inside", () => {
    expect(wrapTab(1, 4, false)).toBeNull();
    expect(wrapTab(2, 4, true)).toBeNull();
  });

  it("handles a focused heading before or after the controls", () => {
    // Focus on the sheet's heading, before the first control.
    expect(wrapTab(-0.5, 4, false)).toBeNull();
    expect(wrapTab(-0.5, 4, true)).toBe(3);
    // A focused element after the last control.
    expect(wrapTab(3.5, 4, false)).toBe(0);
    expect(wrapTab(3.5, 4, true)).toBeNull();
  });

  it("brings focus back from outside, and holds it without controls", () => {
    expect(wrapTab(NaN, 4, false)).toBe(0);
    expect(wrapTab(NaN, 4, true)).toBe(3);
    expect(wrapTab(0, 0, false)).toBe(-1);
  });
});

describe("modal sheet: where each panel covers the page", () => {
  it("names a media query per panel", () => {
    // Phones and 200% zoom (1280x720 is 640x360 CSS px); from 768 px the
    // inspector and the simulation are columns beside the canvas (W3-6, W3-8).
    expect(sheetWhen.inspector).toBe("(max-width: 767px)");
    expect(sheetWhen.simulation).toBe("(max-width: 767px)");
    // The palette is a popover and the detail floats over the list below 1025.
    expect(sheetWhen.palette).toBe("(max-width: 1024px)");
    expect(sheetWhen.detail).toBe("(max-width: 1024px)");
  });
});
