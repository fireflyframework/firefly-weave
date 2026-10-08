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
import { DROP_REFUSED, dropOutcome } from "../src/app/editor/canvas/handles";
import { layoutLtr } from "../src/app/editor/canvas/layout-ltr";
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

describe("letting go of a handle's edge", () => {
  it("opens the step picker on empty canvas", () => {
    expect(dropOutcome({ x: 450, y: 420 }, layout, 1)).toBe("open");
  });

  it("is refused on or near a step, a handle or an empty-lane slot", () => {
    expect(dropOutcome({ x: 370, y: 140 }, layout, 1)).toBe("refused");
    expect(dropOutcome({ x: 370, y: 192 + 59 }, layout, 1)).toBe("refused");
    expect(dropOutcome({ x: 1056, y: 800 + 60 }, layout, 1)).toBe("refused");
    expect(dropOutcome({ x: 1000, y: 144 }, layout, 1)).toBe("refused");
  });

  it("measures the 60 px on screen, so zooming out widens the reach", () => {
    expect(dropOutcome({ x: 370, y: 192 + 100 }, layout, 1)).toBe("open");
    expect(dropOutcome({ x: 370, y: 192 + 100 }, layout, 0.5)).toBe("refused");
  });

  it("says why", () => {
    expect(DROP_REFUSED).toBe("Steps run in order. Use + to insert.");
  });
});
