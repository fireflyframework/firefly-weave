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
import { beforeAll, describe, expect, it } from "vitest";
import { loadKindRegistrations } from "../src/app/editor/ndv/kinds";
import { COMMON_ACTIONS } from "../src/app/editor/ndv/owned/common-actions";
import { insertOwnedStep } from "../src/app/editor/ndv/owned/insert-owned";
import {
  isFrozen,
  newHttpRecipe,
  type OwnedRecipe,
} from "../src/app/editor/ndv/owned/owned-actions";
import {
  ownedActionsOf,
  renameOwnedAction,
  usesOwning,
  withOwnedAction,
  withOwnedVersion,
} from "../src/app/editor/ndv/owned/owned-store";
import {
  driftedOwned,
  forkEdited,
  inputHash,
  markPublished,
  moveOwned,
  ownedExportNote,
  pendingOwned,
  publishKey,
  publishedVersionsOf,
  stableJson,
  versionMoveText,
} from "../src/app/editor/ndv/owned/owned-versions";
import { emptyCanvas } from "../src/app/editor/state/canvas-sidecar";
import { StructuredCanvasAdapter } from "../src/app/model";

const API = "order-intake.call-api@1.0.0";
const EMAIL = "order-intake.send-email@1.0.0";
const action = (id: string) => COMMON_ACTIONS.find((item) => item.id === id)!;
const usesOf = (model: StructuredCanvasAdapter, id: string) =>
  model.nodes().find((node) => node.step.id === id)!.step["uses"];
const withRecipe = (
  model: StructuredCanvasAdapter,
  uses: string,
  change: Record<string, unknown>,
) =>
  withOwnedAction(model.canvas, uses, {
    ...ownedActionsOf(model.canvas)[uses],
    ...change,
  } as OwnedRecipe);

/** order-intake: Call an API (GET /orders?status=open), then two Send email steps sharing one action. */
function workflow() {
  const model = new StructuredCanvasAdapter();
  model.definition.metadata.name = "order-intake";
  insertOwnedStep(model, action("send-email"), "root", 0);
  insertOwnedStep(model, action("send-email"), "root", 1);
  insertOwnedStep(model, action("call-api"), "root", 0);
  model.canvas = withRecipe(model, API, { pathTemplate: "/orders" });
  const step = model.nodes().find((node) => node.step.id === "call-api")!.step;
  model.update(
    "call-api",
    JSON.stringify({
      ...step,
      with: { object: { query: { object: { status: { literal: "open" } } } } },
    }),
  );
  model.clearHistory();
  return model;
}

beforeAll(async () => {
  await loadKindRegistrations();
});

describe("versions of a workflow's own actions", () => {
  it("keeps each owned action at its current version, 1.0.0 until it has another", () => {
    const canvas = withOwnedAction(
      emptyCanvas(),
      "flow.get@1.0.0",
      newHttpRecipe(),
    );
    expect(canvas.actionRecipes!["flow.get"]).not.toHaveProperty("version");
    const next = withOwnedVersion(canvas, "flow.get", "1.0.1");
    expect(Object.keys(ownedActionsOf(next))).toEqual(["flow.get@1.0.1"]);
    expect(usesOwning(next, "flow.get@1.0.1")).toBe(true);
    expect(usesOwning(next, "flow.get@1.0.0")).toBe(false);
    expect(withOwnedAction(next, "flow.get@1.0.0", null)).toBe(next);
    expect(withOwnedVersion(next, "flow.get", "1.0.1")).toBe(next);
    expect(() =>
      withOwnedAction(next, "flow.get@1.0.0", newHttpRecipe()),
    ).toThrow("Only version 1.0.1 of flow.get belongs to this workflow.");
    expect(() =>
      withOwnedAction(emptyCanvas(), "flow.new@2.0.0", newHttpRecipe()),
    ).toThrow("A new action of this workflow starts at version 1.0.0.");
    expect(() => withOwnedVersion(canvas, "flow.get", "next")).toThrow(
      "Use a version such as 1.0.1.",
    );
    expect(() => withOwnedVersion(canvas, "flow.other", "1.0.1")).toThrow(
      "flow.other isn't an action this workflow owns.",
    );
  });
  it("lists the actions to publish in step order, once each, and none once published", () => {
    const model = workflow();
    expect(pendingOwned(model)).toEqual([API, EMAIL]);
    model.canvas = markPublished(model, EMAIL);
    expect(pendingOwned(model)).toEqual([API]);
  });
  it("records a publication with its version and fingerprint, keeping an API action's description as published", () => {
    const model = workflow();
    model.canvas = markPublished(model, API);
    const recipe = ownedActionsOf(model.canvas)[API];
    expect(recipe).toMatchObject({
      description: "Used by step call-api of order-intake",
      published: { version: "1.0.0" },
    });
    expect(recipe.published!.input).toMatch(/^[0-9a-f]{14}$/);
    expect(isFrozen(recipe)).toBe(true);
    expect(driftedOwned(model)).toEqual([]);
    expect(markPublished(model, "nothing@1.0.0")).toBe(model.canvas);
  });
  it("gives a changed published action its next version for every step that calls it, in one undo step", () => {
    const model = workflow();
    model.canvas = markPublished(model, EMAIL);
    const moves = model.batch(() => {
      model.canvas = withRecipe(model, EMAIL, { timeoutSeconds: 20 });
      return forkEdited(model, () => []);
    });
    const next = "order-intake.send-email@1.0.1";
    expect(moves).toEqual([{ from: EMAIL, to: next }]);
    expect([
      usesOf(model, "send-email"),
      usesOf(model, "send-email-2"),
    ]).toEqual([next, next]);
    expect(ownedActionsOf(model.canvas)[next]).toMatchObject({
      version: "1.0.1",
      timeoutSeconds: 20,
      published: { version: "1.0.0" },
    });
    expect(pendingOwned(model)).toEqual([API, next]);
    expect(versionMoveText(moves[0])).toBe(
      "order-intake.send-email 1.0.0 is published, so this change starts version 1.0.1.",
    );
    model.undo();
    expect([
      usesOf(model, "send-email"),
      usesOf(model, "send-email-2"),
    ]).toEqual([EMAIL, EMAIL]);
    expect(isFrozen(ownedActionsOf(model.canvas)[EMAIL])).toBe(true);
  });
  it("skips versions the workspace already has", () => {
    const model = workflow();
    model.canvas = markPublished(model, EMAIL);
    model.canvas = withRecipe(model, EMAIL, { timeoutSeconds: 20 });
    const rows = [
      { name: "order-intake.send-email", version: "1.0.1" },
      { name: "other", version: "1.0.2" },
    ];
    expect(publishedVersionsOf(rows, "order-intake.send-email")).toEqual([
      "1.0.1",
    ]);
    expect(
      model.batch(() =>
        forkEdited(model, (name) => publishedVersionsOf(rows, name)),
      ),
    ).toEqual([{ from: EMAIL, to: "order-intake.send-email@1.0.2" }]);
  });
  it("doesn't start a version for a renamed step, a changed value or an unchanged recipe, and does for a new query name", () => {
    const model = workflow();
    model.canvas = markPublished(model, API);
    model.renameStep("call-api", "get-orders");
    expect(driftedOwned(model)).toEqual([]);
    const step = () =>
      model.nodes().find((node) => node.step.id === "get-orders")!.step;
    const query = (key: string, value: string) =>
      model.update(
        "get-orders",
        JSON.stringify({
          ...step(),
          with: {
            object: { query: { object: { [key]: { literal: value } } } },
          },
        }),
      );
    query("status", "closed");
    expect(driftedOwned(model)).toEqual([]);
    model.canvas = withRecipe(model, API, {});
    expect(driftedOwned(model)).toEqual([]);
    query("state", "open");
    expect(driftedOwned(model)).toEqual([API]);
    expect(driftedOwned(model, new Set(["other@1.0.0"]))).toEqual([]);
  });
  it("moves an owned action to a free name or its next version, with the steps that call it", () => {
    const model = workflow();
    model.batch(() =>
      moveOwned(model, EMAIL, "order-intake.send-email-2@1.0.0"),
    );
    expect([
      usesOf(model, "send-email"),
      usesOf(model, "send-email-2"),
    ]).toEqual([
      "order-intake.send-email-2@1.0.0",
      "order-intake.send-email-2@1.0.0",
    ]);
    expect(Object.keys(ownedActionsOf(model.canvas)).sort()).toEqual([
      API,
      "order-intake.send-email-2@1.0.0",
    ]);
    model.canvas = markPublished(model, API);
    model.batch(() => moveOwned(model, API, "order-intake.call-api@1.0.1"));
    expect(usesOf(model, "call-api")).toBe("order-intake.call-api@1.0.1");
    expect(() =>
      model.batch(() => moveOwned(model, "nothing@1.0.0", "x@1.0.0")),
    ).toThrow("nothing@1.0.0 isn't an action this workflow owns.");
  });
  it("won't rename a published action, or change an action's version by renaming it", () => {
    const model = workflow();
    model.canvas = markPublished(model, API);
    expect(() =>
      renameOwnedAction(model.canvas, API, "order-intake.fetch@1.0.0"),
    ).toThrow("A published action keeps its name.");
    expect(() =>
      renameOwnedAction(model.canvas, EMAIL, "order-intake.mail@2.0.0"),
    ).toThrow("A renamed action keeps its version.");
  });
  it("makes stable fingerprints and keys", () => {
    expect(stableJson({ b: 1, a: { d: [1, { f: 1, e: 2 }], c: 2 } })).toBe(
      stableJson({ a: { c: 2, d: [1, { e: 2, f: 1 }] }, b: 1 }),
    );
    expect(inputHash("a")).toMatch(/^[0-9a-f]{14}$/);
    expect(inputHash("a")).not.toBe(inputHash("b"));
    expect(publishKey(API, "kind: Action\n")).toBe(
      publishKey(API, "kind: Action\n"),
    );
    expect(publishKey(API, "kind: Action\n")).not.toBe(
      publishKey(API, "kind: Action\nx: 1\n"),
    );
    expect(publishKey(API, "x")).toMatch(
      /^owned-order-intake\.call-api@1\.0\.0-[0-9a-f]{14}$/,
    );
    expect(
      publishKey(`${"a".repeat(190)}@1.0.0`, "x").length,
    ).toBeLessThanOrEqual(200);
  });
  it("says what Save to file leaves out", () => {
    expect(ownedExportNote(0)).toBe("");
    expect(ownedExportNote(1)).toBe(
      "These files don't keep the settings of 1 action of this workflow's Call an API, email or file steps yet. Publish the workflow to keep it in the project.",
    );
    expect(ownedExportNote(2)).toBe(
      "These files don't keep the settings of 2 actions of this workflow's Call an API, email or file steps yet. Publish the workflow to keep them in the project.",
    );
  });
});
