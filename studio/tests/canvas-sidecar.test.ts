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
  CANVAS_KIND,
  CANVAS_SCHEMA_ID,
  CANVAS_TOO_BIG,
  MAX_OWNED_ACTIONS,
  MAX_STEP_NOTE_TEXT,
  MAX_TRIGGERS,
  canvasBytes,
  canvasFile,
  canvasFromStored,
  emptyCanvas,
  ownedRecipes,
  parseCanvas,
  pruneCanvas,
  pruneOwnedRecipes,
  readCanvas,
  renameInCanvas,
  renameOwnedRecipe,
  stepNoteLine,
  stepNotesOf,
  triggerIntents,
  withOwnedRecipe,
  withStepNote,
  withTriggerIntents,
  type ActionRecipe,
  type CanvasSidecar,
  type TriggerIntent,
} from "../src/app/editor/state/canvas-sidecar";
import { freshWorkflow } from "../src/app/model";

const http: ActionRecipe = {
  kind: "http",
  method: "GET",
  pathTemplate: "/orders",
  statuses: [200],
  timeoutSeconds: 30,
};
const full = (): CanvasSidecar =>
  withTriggerIntents(
    withStepNote(
      withOwnedRecipe(emptyCanvas(), "order-intake.get-orders", http),
      "get-orders",
      {
        text: "Calls the sandbox.\nAsk Ana first.",
        showOnCanvas: true,
      },
    ),
    [{ id: "trigger-0a1b2c3d", kind: "manual" }],
  );

describe("the canvas sidecar", () => {
  it("names its schema and starts empty", () => {
    expect(CANVAS_SCHEMA_ID).toBe("urn:firefly-weave:schema:studio-canvas:v2");
    expect(emptyCanvas()).toEqual({ schemaVersion: 2, kind: CANVAS_KIND });
  });
  it("round-trips through its file", () => {
    const canvas = full();
    const file = canvasFile(canvas, "sha256:abc");
    const read = parseCanvas(JSON.parse(file));
    expect(read).toEqual({
      ok: true,
      canvas: { ...canvas, semanticDigest: "sha256:abc" },
    });
    expect(file.endsWith("\n")).toBe(true);
  });
  it("reads today's layout file as version 1: the viewport stays, positions go", () => {
    const layout = {
      schemaVersion: 1,
      revision: 3,
      positions: { a: { x: 1, y: 2 } },
      viewport: { x: 10, y: 20, zoom: 1.5 },
    };
    expect(readCanvas(layout)).toEqual({
      ok: true,
      canvas: {
        schemaVersion: 2,
        kind: CANVAS_KIND,
        viewport: { x: 10, y: 20, zoom: 1.5 },
      },
    });
    const stored = {
      revision: 0,
      positions: {},
      viewport: { x: 0, y: 0, zoom: 1 },
    };
    expect(readCanvas(stored)).toEqual({
      ok: true,
      canvas: { ...emptyCanvas(), viewport: { x: 0, y: 0, zoom: 1 } },
    });
    expect(readCanvas(null)).toEqual({ ok: true, canvas: emptyCanvas() });
    expect(readCanvas({ hello: 1 })).toEqual({
      ok: false,
      problems: ["/: this isn't a Studio canvas file."],
    });
  });
  it("says what is wrong with a file, field by field", () => {
    const read = parseCanvas({
      schemaVersion: 2,
      kind: CANVAS_KIND,
      positions: {},
      ownedActions: ["ok.name", "-bad"],
      actionRecipes: {
        "not.listed": { kind: "http" },
        "ok.name": { kind: "soap" },
      },
    });
    expect(read.ok).toBe(false);
    if (read.ok) return;
    expect(read.problems).toEqual([
      "/positions: remove this field; a canvas file doesn't have it.",
      "/ownedActions/1: use letters, numbers, dots, underscores or hyphens, starting with a letter or number.",
      "/actionRecipes/not.listed: list this action in ownedActions first.",
      "/actionRecipes/ok.name/kind: use http or connector.",
    ]);
    expect(parseCanvas({ schemaVersion: 3, kind: CANVAS_KIND })).toMatchObject({
      ok: false,
    });
  });
  it("keeps step notes and puts the first line on the canvas only when asked", () => {
    const canvas = full();
    expect(stepNotesOf(canvas)["get-orders"].text).toContain("sandbox");
    expect(stepNoteLine(canvas, "get-orders")).toBe("Calls the sandbox.");
    expect(
      stepNoteLine(
        withStepNote(canvas, "get-orders", { text: "Hidden" }),
        "get-orders",
      ),
    ).toBe("");
    expect(stepNotesOf(withStepNote(canvas, "get-orders", null))).toEqual({});
    expect(
      withStepNote(canvas, "get-orders", { text: "   " }).stepNotes,
    ).toBeUndefined();
  });
  it("keeps owned actions by name with their recipes", () => {
    const canvas = full();
    expect(canvas.ownedActions).toEqual(["order-intake.get-orders"]);
    expect(ownedRecipes(canvas)).toEqual({ "order-intake.get-orders": http });
    const renamed = renameOwnedRecipe(
      canvas,
      "order-intake.get-orders",
      "order-intake.fetch-orders",
    );
    expect(renamed.ownedActions).toEqual(["order-intake.fetch-orders"]);
    expect(Object.keys(ownedRecipes(renamed))).toEqual([
      "order-intake.fetch-orders",
    ]);
    const gone = withOwnedRecipe(canvas, "order-intake.get-orders", null);
    expect(gone.ownedActions).toBeUndefined();
    expect(gone.actionRecipes).toBeUndefined();
    expect(
      pruneOwnedRecipes(canvas, new Set(["order-intake.get-orders"])),
    ).toBe(canvas);
    expect(pruneOwnedRecipes(canvas, new Set()).ownedActions).toBeUndefined();
  });
  it("follows renamed steps and forgets the notes of removed ones, keeping trigger notes", () => {
    const canvas = withStepNote(
      withStepNote(full(), "trigger-0a1b2c3d", { text: "Started by hand" }),
      "$trigger",
      {
        text: "Fill in the order",
      },
    );
    const renamed = renameInCanvas(canvas, "get-orders", "fetch-orders");
    expect(Object.keys(stepNotesOf(renamed))).toEqual([
      "fetch-orders",
      "trigger-0a1b2c3d",
      "$trigger",
    ]);
    expect(renameInCanvas(canvas, "nothing", "else")).toBe(canvas);
    expect(Object.keys(stepNotesOf(pruneCanvas(canvas, new Set())))).toEqual([
      "trigger-0a1b2c3d",
      "$trigger",
    ]);
    expect(pruneCanvas(canvas, new Set(["get-orders"]))).toBe(canvas);
  });
  it("stores trigger choices", () => {
    expect(triggerIntents(full())).toEqual([
      { id: "trigger-0a1b2c3d", kind: "manual" },
    ]);
    expect(withTriggerIntents(full(), []).triggers).toBeUndefined();
  });
  it("opens a stored canvas for its workflow, dropping notes of steps it doesn't have", () => {
    const stored = JSON.parse(canvasFile(full(), "sha256:abc"));
    const opened = canvasFromStored(stored, freshWorkflow(), new Set());
    expect(opened.problems).toEqual([]);
    expect(opened.dropped).toBe(true);
    expect(opened.canvas.stepNotes).toBeUndefined();
    expect(ownedRecipes(opened.canvas)).toEqual({
      "order-intake.get-orders": http,
    });
    const broken = canvasFromStored(
      { schemaVersion: 2 },
      freshWorkflow(),
      new Set(),
    );
    expect(broken.canvas).toEqual(emptyCanvas());
    expect(broken.problems.length).toBeGreaterThan(0);
  });
  it("measures itself against the 256 KB cap", () => {
    expect(canvasBytes(emptyCanvas())).toBe(
      JSON.stringify(emptyCanvas()).length,
    );
    expect(CANVAS_TOO_BIG).toBe(
      "The canvas can hold 256 KB of notes and settings. Shorten or delete a note first.",
    );
  });
});

const NAME_ERROR =
  "Use letters, numbers, dots, underscores or hyphens for the action name, starting with a letter or number.";
const triggerId = (n: number) => `trigger-${n.toString(16).padStart(8, "0")}`;
const trigger = (n: number): TriggerIntent => ({
  id: triggerId(n),
  kind: "manual",
});

describe("the setters return the canvas itself when nothing changes", () => {
  it("leaves step notes alone when the note is the same or absent", () => {
    const canvas = full();
    const same = {
      text: "Calls the sandbox.\nAsk Ana first.",
      showOnCanvas: true,
    };
    expect(withStepNote(canvas, "get-orders", same)).toBe(canvas);
    expect(withStepNote(canvas, "absent", null)).toBe(canvas);
    expect(withStepNote(canvas, "absent", { text: "  " })).toBe(canvas);
    expect(withStepNote(canvas, "toString", null)).toBe(canvas);
    expect(withStepNote(canvas, "get-orders", { text: "Changed" })).not.toBe(
      canvas,
    );
  });
  it("leaves owned actions alone when the recipe is the same or absent", () => {
    const canvas = full();
    const reordered: ActionRecipe = {
      timeoutSeconds: 30,
      statuses: [200],
      pathTemplate: "/orders",
      method: "GET",
      kind: "http",
    };
    expect(withOwnedRecipe(canvas, "order-intake.get-orders", reordered)).toBe(
      canvas,
    );
    expect(withOwnedRecipe(canvas, "absent.name", null)).toBe(canvas);
    const empty = emptyCanvas();
    expect(withOwnedRecipe(empty, "absent.name", null)).toBe(empty);
    expect(renameOwnedRecipe(canvas, "absent.name", "other.name")).toBe(canvas);
    expect(
      renameOwnedRecipe(
        canvas,
        "order-intake.get-orders",
        "order-intake.get-orders",
      ),
    ).toBe(canvas);
  });
  it("leaves trigger choices alone when the list is the same", () => {
    const canvas = full();
    expect(
      withTriggerIntents(canvas, [{ kind: "manual", id: "trigger-0a1b2c3d" }]),
    ).toBe(canvas);
    const empty = emptyCanvas();
    expect(withTriggerIntents(empty, [])).toBe(empty);
    expect(withTriggerIntents(canvas, [])).not.toBe(canvas);
  });
  it("keeps an action where it is listed when its recipe changes", () => {
    const two = withOwnedRecipe(
      withOwnedRecipe(emptyCanvas(), "a.one", http),
      "a.two",
      http,
    );
    const changed = withOwnedRecipe(two, "a.one", { ...http, method: "POST" });
    expect(changed.ownedActions).toEqual(["a.one", "a.two"]);
    expect(Object.keys(ownedRecipes(changed))).toEqual(["a.one", "a.two"]);
    expect(ownedRecipes(changed)["a.one"]["method"]).toBe("POST");
    expect(ownedRecipes(two)["a.one"]["method"]).toBe("GET");
  });
  it("renames an action that has no recipe yet, and leaves no empty recipes behind", () => {
    const listed = { ...emptyCanvas(), ownedActions: ["a.one", "a.two"] };
    const renamed = renameOwnedRecipe(listed, "a.one", "a.uno");
    expect(renamed.ownedActions).toEqual(["a.uno", "a.two"]);
    expect("actionRecipes" in renamed).toBe(false);
  });
});

describe("the setters refuse or clamp what a canvas file wouldn't hold", () => {
  it("cuts a step note to its longest text, counting characters", () => {
    const long = "😀".repeat(MAX_STEP_NOTE_TEXT + 5);
    const note = stepNotesOf(withStepNote(emptyCanvas(), "a", { text: long }))[
      "a"
    ];
    expect([...note.text]).toHaveLength(MAX_STEP_NOTE_TEXT);
    const same = withStepNote(emptyCanvas(), "a", {
      text: "😀".repeat(MAX_STEP_NOTE_TEXT),
    });
    expect(withStepNote(same, "a", { text: long })).toBe(same);
  });
  it("leaves out a showOnCanvas that isn't true or false", () => {
    const note = { text: "x", showOnCanvas: "yes" } as unknown as {
      text: string;
    };
    expect(stepNotesOf(withStepNote(emptyCanvas(), "a", note))["a"]).toEqual({
      text: "x",
    });
  });
  it("refuses an action name or a recipe a canvas file wouldn't read", () => {
    for (const bad of ["", "-bad", ".bad", "has space", "a/b", "__proto__ "])
      expect(() => withOwnedRecipe(emptyCanvas(), bad, http)).toThrow(
        NAME_ERROR,
      );
    expect(() =>
      withOwnedRecipe(emptyCanvas(), "ok.name", { kind: "soap" } as never),
    ).toThrow("Use http or connector for the action's recipe.");
    expect(
      withOwnedRecipe(emptyCanvas(), "ok.name", { kind: "connector" })
        .ownedActions,
    ).toEqual(["ok.name"]);
  });
  it("refuses a 201st action but lets a listed one change", () => {
    let canvas = emptyCanvas();
    for (let n = 0; n < MAX_OWNED_ACTIONS; n++)
      canvas = withOwnedRecipe(canvas, `action-${n}`, http);
    expect(() => withOwnedRecipe(canvas, "one-more", http)).toThrow(
      `Keep at most ${MAX_OWNED_ACTIONS} actions.`,
    );
    expect(
      withOwnedRecipe(canvas, "action-7", { ...http, method: "PUT" }),
    ).not.toBe(canvas);
    expect(parseCanvas(JSON.parse(canvasFile(canvas, "sha256:abc"))).ok).toBe(
      true,
    );
  });
  it("refuses a rename onto a taken or invalid name, and keeps the canvas as it was", () => {
    const two = withOwnedRecipe(
      withOwnedRecipe(emptyCanvas(), "a.one", http),
      "a.two",
      http,
    );
    expect(() => renameOwnedRecipe(two, "a.one", "a.two")).toThrow(
      "Another action is already named a.two.",
    );
    expect(() => renameOwnedRecipe(two, "a.one", "-nope")).toThrow(NAME_ERROR);
    expect(two.ownedActions).toEqual(["a.one", "a.two"]);
    expect(renameOwnedRecipe(two, "a.one", "a.uno").ownedActions).toEqual([
      "a.uno",
      "a.two",
    ]);
  });
  it("refuses trigger IDs and kinds a canvas file wouldn't read, and keeps six", () => {
    const id =
      'Give each trigger an ID of "trigger-" and 8 lowercase hexadecimal digits.';
    expect(() =>
      withTriggerIntents(emptyCanvas(), [
        { id: "trigger-XYZ", kind: "manual" },
      ]),
    ).toThrow(id);
    expect(() =>
      withTriggerIntents(emptyCanvas(), [{ id: "t-1", kind: "manual" }]),
    ).toThrow(id);
    expect(() =>
      withTriggerIntents(emptyCanvas(), [trigger(1), trigger(1)]),
    ).toThrow("Give each trigger its own ID.");
    expect(() =>
      withTriggerIntents(emptyCanvas(), [
        { id: triggerId(2), kind: "carrier-pigeon" as never },
      ]),
    ).toThrow(
      "Use manual, webhook, schedule, broker, email, provider or called for a trigger.",
    );
    const many = withTriggerIntents(
      emptyCanvas(),
      Array.from({ length: MAX_TRIGGERS + 3 }, (_, n) => trigger(n)),
    );
    expect(triggerIntents(many)).toHaveLength(MAX_TRIGGERS);
  });
  it("copies what it stores, so later changes to the argument don't reach the canvas", () => {
    const intent: TriggerIntent = {
      id: triggerId(3),
      kind: "webhook",
      options: { path: "/hook" },
    };
    const canvas = withTriggerIntents(emptyCanvas(), [intent]);
    (intent["options"] as { path: string }).path = "/changed";
    expect(triggerIntents(canvas)[0]["options"]).toEqual({ path: "/hook" });
  });
  it("refuses a write past the size cap, and still lets the canvas shrink", () => {
    let canvas = emptyCanvas();
    // Adds notes until the canvas refuses one; true when it did.
    const fill = (id: (n: number) => string, text: string) => {
      for (let n = 0; n < 500; n++) {
        try {
          canvas = withStepNote(canvas, id(n), { text });
        } catch (error) {
          expect(error).toEqual(new Error(CANVAS_TOO_BIG));
          return true;
        }
      }
      return false;
    };
    expect(fill((n) => `step-${n}`, "é".repeat(MAX_STEP_NOTE_TEXT))).toBe(true);
    expect(fill((n) => `t${n}`, "x")).toBe(true);
    // Full to within one small note, with room left for the digest a file adds.
    expect(canvasBytes(canvas)).toBeLessThanOrEqual(262_144 - 128);
    expect(canvasBytes(canvas)).toBeGreaterThan(262_144 - 128 - 80);
    expect(() => withTriggerIntents(canvas, [trigger(1)])).toThrow(
      CANVAS_TOO_BIG,
    );
    expect(() => withOwnedRecipe(canvas, "a.one", http)).toThrow(
      CANVAS_TOO_BIG,
    );
    // Shrinking and removing always work.
    expect(() =>
      withStepNote(canvas, "step-0", { text: "Short" }),
    ).not.toThrow();
    expect(withStepNote(canvas, "step-0", null)).not.toBe(canvas);
    const file = canvasFile(canvas, `sha256:${"f".repeat(64)}`);
    expect(parseCanvas(JSON.parse(file)).ok).toBe(true);
  });
  it("reads back, without a problem, every canvas the setters built", () => {
    const digest = `sha256:${"0".repeat(64)}`;
    let busy = emptyCanvas();
    for (let n = 0; n < MAX_OWNED_ACTIONS; n++)
      busy = withOwnedRecipe(busy, `action-${n}`, http);
    for (let n = 0; n < 20; n++)
      busy = withStepNote(busy, `step-${n}`, {
        text: "😀".repeat(MAX_STEP_NOTE_TEXT * 2),
        showOnCanvas: n % 2 === 0,
      });
    busy = withTriggerIntents(
      busy,
      Array.from({ length: MAX_TRIGGERS }, (_, n) => trigger(n)),
    );
    const built = [
      emptyCanvas(),
      full(),
      renameOwnedRecipe(
        full(),
        "order-intake.get-orders",
        "order-intake.fetch-orders",
      ),
      pruneOwnedRecipes(busy, new Set(["action-1"])),
      renameInCanvas(busy, "step-3", "renamed"),
      busy,
    ];
    for (const canvas of built) {
      const read = parseCanvas(JSON.parse(canvasFile(canvas, digest)));
      expect(read).toEqual({
        ok: true,
        canvas: { ...canvas, semanticDigest: digest },
      });
    }
  });
});
