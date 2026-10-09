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
import { readFileSync } from "node:fs";
import { parse } from "yaml";
import { describe, expect, it } from "vitest";
import { layoutLtr } from "../src/app/editor/canvas/layout-ltr";
import { neighbor, placesOf } from "../src/app/editor/canvas/navigation";
import { builtInKinds } from "../src/app/editor/ndv/kinds/builtin";
import type { Workflow } from "../src/app/model";

const roles = new Map(builtInKinds.map((kind) => [kind.kind, kind.role]));
const layout = layoutLtr(
  parse(
    readFileSync(
      new URL("./fixtures/vendor-payment-approval.yaml", import.meta.url),
      "utf8",
    ),
  ) as Workflow,
  {
    role: (kind) => roles.get(kind),
    triggers: [{ id: "manual", kind: "manual" }],
  },
);

describe("arrow keys on the canvas", () => {
  it("→ follows the flow: into a group's first lane, then past the group to End", () => {
    expect(
      [
        "$trigger:manual",
        "prepare-request",
        "route",
        "pay-and-notify",
        "post-ledger-entry",
        "rejected",
        "record-result",
        "$end",
      ].map((id) => neighbor(layout, id, "next")),
    ).toEqual([
      "prepare-request",
      "approval",
      "pay-vendor",
      "post-ledger-entry",
      "record-result",
      "record-result",
      "$end",
      null,
    ]);
  });

  it("← goes back: to the group from its first lane step, and to the trigger from the first step", () => {
    expect(
      [
        "approval",
        "pay-vendor",
        "post-ledger-entry",
        "prepare-request",
        "$end",
        "$trigger:manual",
      ].map((id) => neighbor(layout, id, "previous")),
    ).toEqual([
      "prepare-request",
      "route",
      "pay-and-notify",
      "$trigger:manual",
      "record-result",
      null,
    ]);
  });

  it("↑ and ↓ move to the nearest step in the lane above or below in the same group", () => {
    expect(neighbor(layout, "pay-vendor", "below")).toBe("rejected");
    expect(neighbor(layout, "pay-and-notify", "below")).toBe("rejected");
    expect(neighbor(layout, "rejected", "above")).toBe("pay-vendor");
    expect(neighbor(layout, "post-ledger-entry", "below")).toBe(
      "send-confirmation",
    );
    expect(neighbor(layout, "send-confirmation", "below")).toBeNull();
    expect(neighbor(layout, "rejected", "below")).toBeNull();
    expect(neighbor(layout, "approval", "above")).toBeNull();
  });

  it("knows each step's sequence, index and group, and nothing about the trigger and End", () => {
    const places = placesOf(layout);
    expect(places.get("send-confirmation")).toEqual({
      id: "send-confirmation",
      owner: "pay-and-notify/email",
      index: 0,
      parent: "pay-and-notify",
      order: 7,
    });
    expect(places.has("$trigger:manual")).toBe(false);
    expect(places.has("$end")).toBe(false);
    expect(places.size).toBe(9);
  });
});
