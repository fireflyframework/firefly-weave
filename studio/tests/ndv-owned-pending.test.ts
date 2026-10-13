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
  newConnectorRecipe,
  newHttpRecipe,
} from "../src/app/editor/ndv/owned/owned-actions";
import {
  OWNED_UNPUBLISHED,
  withOwnedPending,
} from "../src/app/editor/ndv/owned/owned-pending";
import { withOwnedAction } from "../src/app/editor/ndv/owned/owned-store";
import { emptyCanvas } from "../src/app/editor/state/canvas-sidecar";

const definition = {
  spec: {
    steps: [
      { id: "call-api", kind: "action", uses: "order-intake.call-api@1.0.0" },
      { id: "lookup", kind: "action", uses: "crm.lookup@2.0.0" },
      { id: "send", kind: "action", uses: "order-intake.send-email@1.0.0" },
    ],
  },
};
const unknown = (index: number) => ({
  code: "WV-COMP-UNKNOWN_ACTION",
  severity: "error",
  path: `/spec/steps/${index}/uses`,
  message: "Unknown action",
});
const result = () => ({
  ok: false,
  validationOk: true,
  errorCount: 3,
  diagnostics: [unknown(0), unknown(1), unknown(2)],
});
const canvas = withOwnedAction(
  withOwnedAction(emptyCanvas(), "order-intake.call-api@1.0.0", {
    ...newHttpRecipe(),
    pathTemplate: "/orders",
  }),
  "order-intake.send-email@1.0.0",
  {
    ...newConnectorRecipe("weave-email@1.0.0", "send"),
    published: { version: "1.0.0", input: "0123456789abcd" },
  },
);

describe("a platform check of steps whose own actions aren't published yet", () => {
  it("turns their unknown-action errors into notes and counts them, keeping every other error", () => {
    const checked = withOwnedPending(result(), definition, canvas);
    expect(checked.errorCount).toBe(2);
    expect(checked.ownedPending).toBe(1);
    expect(checked.diagnostics[0]).toEqual({
      ...unknown(0),
      code: OWNED_UNPUBLISHED,
      severity: "info",
      message:
        "This step's action is published with the workflow: Publish… and Simulate publish it first.",
    });
    // Another workflow's action, and a published owned action the catalog lacks, still block.
    expect(checked.diagnostics.slice(1)).toEqual([unknown(1), unknown(2)]);
  });
  it("returns the same result when nothing is pending or nothing matches", () => {
    const plain = result();
    expect(withOwnedPending(plain, definition, emptyCanvas())).toBe(plain);
    expect(withOwnedPending(plain, { spec: { steps: [] } }, canvas)).toBe(
      plain,
    );
    expect(withOwnedPending(plain, null, canvas)).toBe(plain);
  });
  it("passes a check whose only errors were those actions, as the platform counts a pass", () => {
    const failed = {
      ok: false,
      validationOk: false,
      errorCount: 1,
      diagnostics: [unknown(0)],
    };
    expect(withOwnedPending(failed, definition, canvas)).toMatchObject({
      validationOk: true,
      errorCount: 0,
      ok: false,
      ownedPending: 1,
    });
    const other = {
      ...failed,
      errorCount: 2,
      diagnostics: [unknown(0), unknown(1)],
    };
    expect(withOwnedPending(other, definition, canvas)).toMatchObject({
      validationOk: false,
      errorCount: 1,
      ownedPending: 1,
    });
  });
});
