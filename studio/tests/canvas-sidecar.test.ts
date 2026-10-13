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
  MAX_NOTES,
  MAX_OWNED_ACTIONS,
  MAX_STEP_NOTE_TEXT,
  MAX_TRIGGERS,
  NOTE_SIZES,
  addNote,
  addTrigger,
  canvasBytes,
  canvasFile,
  canvasFromStored,
  emptyCanvas,
  ownedRecipes,
  parseCanvas,
  pruneCanvas,
  pruneOwnedRecipes,
  readCanvas,
  removeNote,
  removeTrigger,
  renameInCanvas,
  renameOwnedRecipe,
  stepNoteLine,
  stepNotesOf,
  syncCalled,
  triggerInputs,
  triggerIntents,
  updateNote,
  withOwnedRecipe,
  withStepNote,
  withTriggerIntents,
  type ActionRecipe,
  type CanvasSidecar,
  type StickyNote,
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
  it("reads an owned action's version and publication, and refuses malformed ones", () => {
    const base = { schemaVersion: 2, kind: CANVAS_KIND, ownedActions: ["a.b"] };
    const good = {
      kind: "http",
      version: "1.0.1",
      published: { version: "1.0.0", input: "0123456789abcd" },
    };
    expect(parseCanvas({ ...base, actionRecipes: { "a.b": good } })).toEqual({
      ok: true,
      canvas: { ...base, actionRecipes: { "a.b": good } },
    });
    expect(
      parseCanvas({
        ...base,
        actionRecipes: {
          "a.b": {
            kind: "http",
            version: "one",
            published: { version: "1.0.0" },
          },
        },
      }),
    ).toEqual({
      ok: false,
      problems: [
        "/actionRecipes/a.b/version: use a version such as 1.0.1.",
        "/actionRecipes/a.b/published: remove this field; Studio writes it when it publishes the action.",
      ],
    });
  });
});

const NAME_ERROR =
  "Use letters, numbers, dots, underscores or hyphens for the action name, starting with a letter or number.";
const triggerId = (n: number) => `trigger-${n.toString(16).padStart(8, "0")}`;
const trigger = (n: number): TriggerIntent => ({
  id: triggerId(n),
  kind: "webhook",
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
        { id: triggerId(1), kind: "called" },
        { id: triggerId(2), kind: "called" },
      ]),
    ).toThrow("Keep one manual trigger and one called trigger at most.");
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

/** A random source that returns these hex strings in order. */
const ids =
  (...values: string[]) =>
  () =>
    values.shift() ?? "ffffffff";
const note = (id: string, stepId?: string): StickyNote => ({
  id,
  text: "",
  color: "yellow",
  x: 0,
  y: 0,
  width: 240,
  height: 120,
  ...(stepId ? { anchor: { stepId, dx: 0, dy: 168 } } : {}),
});
const workflowWith = (callable: boolean) => {
  const workflow = freshWorkflow();
  if (callable) workflow.spec["callable"] = {};
  return workflow;
};

describe("sticky notes in the sidecar", () => {
  it("adds a yellow 240 by 120 note, anchored when asked, with a fresh ID", () => {
    const added = addNote(
      emptyCanvas(),
      { x: 10.4, y: 20.6 },
      { stepId: "check", dx: 0.4, dy: 168 },
      ids("0000000c"),
    );
    expect(added).toEqual({
      id: "note-0000000c",
      canvas: {
        ...emptyCanvas(),
        notes: [{ ...note("note-0000000c", "check"), x: 10, y: 21 }],
      },
    });
    const again = addNote(
      added.canvas,
      { x: 0, y: 0 },
      null,
      ids("0000000c", "0000000d"),
    );
    expect(again.id).toBe("note-0000000d");
    expect(NOTE_SIZES.small).toEqual({ width: 240, height: 120 });
  });

  it("keeps notes within their limits and says why it refuses", () => {
    const full: CanvasSidecar = {
      ...emptyCanvas(),
      notes: Array.from({ length: MAX_NOTES }, (_, i) =>
        note(`note-${i.toString(16).padStart(8, "0")}`),
      ),
    };
    expect(() => addNote(full, { x: 0, y: 0 }, null)).toThrow(
      "A canvas holds at most 200 notes.",
    );
    const one: CanvasSidecar = {
      ...emptyCanvas(),
      notes: [note("note-00000001")],
    };
    expect(
      updateNote(one, "note-00000001", { width: 10, height: 5000 }).notes?.[0],
    ).toMatchObject({ width: 160, height: 1200 });
    expect(() =>
      updateNote(one, "note-00000001", { text: "x".repeat(4001) }),
    ).toThrow("A note holds at most 4000 characters.");
    expect(() =>
      updateNote(one, "note-00000001", { color: "orange" as never }),
    ).toThrow("Use yellow, blue, green, pink, purple or gray for a note.");
    expect(() => updateNote(one, "note-0000000f", { text: "a" })).toThrow(
      "That note is already gone.",
    );
    expect(updateNote(one, "note-00000001", { text: "" })).toBe(one);
    expect(removeNote(one, "note-00000001")).toEqual(emptyCanvas());
    expect(removeNote(one, "note-0000000f")).toBe(one);
  });

  it("moves, re-anchors and frees a note", () => {
    const one: CanvasSidecar = {
      ...emptyCanvas(),
      notes: [note("note-00000001", "check")],
    };
    const moved = updateNote(one, "note-00000001", {
      x: 40.6,
      y: 7,
      anchor: { stepId: "check", dx: 40.6, dy: -161 },
    });
    expect(moved.notes?.[0]).toMatchObject({
      x: 41,
      y: 7,
      anchor: { stepId: "check", dx: 41, dy: -161 },
    });
    expect(
      updateNote(moved, "note-00000001", { anchor: null }).notes?.[0],
    ).not.toHaveProperty("anchor");
  });

  it("refuses an edit that would take the canvas over 256 KB, and still lets it shrink", () => {
    const filler = Array.from({ length: 64 }, (_, i) => ({
      ...note(`note-${i.toString(16).padStart(8, "0")}`),
      text: "x".repeat(4000),
    }));
    const added = addNote(
      { ...emptyCanvas(), notes: filler },
      { x: 0, y: 0 },
      null,
      ids("0000ffff"),
    );
    // Save to file adds the workflow's digest: setters leave 128 bytes for it.
    const room = 262_144 - 128 - canvasBytes(added.canvas);
    expect(room).toBeGreaterThan(0);
    expect(room).toBeLessThan(4000);
    expect(
      updateNote(added.canvas, "note-0000ffff", { text: "y".repeat(room) })
        .notes,
    ).toHaveLength(65);
    expect(() =>
      updateNote(added.canvas, "note-0000ffff", { text: "y".repeat(room + 1) }),
    ).toThrow(CANVAS_TOO_BIG);
    expect(removeNote(added.canvas, "note-00000000").notes).toHaveLength(64);
  });

  it("reads back every note canvas these edits build", () => {
    let canvas = addNote(
      emptyCanvas(),
      { x: 1, y: 2 },
      null,
      ids("0000000a"),
    ).canvas;
    canvas = updateNote(canvas, "note-0000000a", {
      text: "## Reviewed\nwith **ops**",
      color: "blue",
      width: 480,
      height: 320,
    });
    expect(parseCanvas(canvas)).toEqual({ ok: true, canvas });
  });
});

describe("trigger choices in the sidecar", () => {
  it("draws the input form for a workflow with steps and no choice yet", () => {
    expect(triggerInputs(emptyCanvas(), true)).toEqual([
      { id: "manual", kind: "manual" },
    ]);
    expect(triggerInputs(emptyCanvas(), false)).toEqual([]);
    expect(
      triggerInputs(
        {
          ...emptyCanvas(),
          triggers: [{ id: "trigger-0a1b2c3d", kind: "email" }],
        },
        true,
      ),
    ).toEqual([{ id: "trigger-0a1b2c3d", kind: "email" }]);
  });

  it("adds a trigger, keeping the input form a workflow with steps already had", () => {
    expect(
      addTrigger(
        emptyCanvas(),
        freshWorkflow(),
        "webhook",
        true,
        ids("0000000a", "0000000b"),
      ),
    ).toEqual({
      id: "trigger-0000000b",
      workflow: null,
      canvas: {
        ...emptyCanvas(),
        triggers: [
          { id: "trigger-0000000a", kind: "manual" },
          { id: "trigger-0000000b", kind: "webhook" },
        ],
      },
    });
    expect(
      addTrigger(
        emptyCanvas(),
        freshWorkflow(),
        "manual",
        false,
        ids("0000000c"),
      ),
    ).toMatchObject({
      id: "trigger-0000000c",
      canvas: { triggers: [{ id: "trigger-0000000c", kind: "manual" }] },
    });
  });

  it("allows one input form, one Called by a workflow and six triggers in all", () => {
    expect(() =>
      addTrigger(emptyCanvas(), freshWorkflow(), "manual", true),
    ).toThrow("This workflow already starts from its input form.");
    let canvas = emptyCanvas();
    for (let i = 0; i < 6; i++)
      canvas = addTrigger(canvas, freshWorkflow(), "webhook", false).canvas;
    expect(() =>
      addTrigger(canvas, freshWorkflow(), "schedule", false),
    ).toThrow("A workflow can have at most 6 triggers.");
    const called = addTrigger(
      emptyCanvas(),
      freshWorkflow(),
      "called",
      false,
    ).canvas;
    expect(() =>
      addTrigger(called, workflowWith(true), "called", false),
    ).toThrow("Other workflows can already call this one.");
  });

  it("makes the workflow callable with its Called by a workflow trigger, and back", () => {
    const added = addTrigger(
      emptyCanvas(),
      workflowWith(false),
      "called",
      false,
    );
    expect(added.workflow?.spec["callable"]).toEqual({});
    const removed = removeTrigger(added.canvas, added.workflow!, added.id);
    expect(removed.canvas).toEqual(emptyCanvas());
    expect(removed.workflow && "callable" in removed.workflow.spec).toBe(false);
    expect(() =>
      removeTrigger(emptyCanvas(), freshWorkflow(), "trigger-00000000"),
    ).toThrow("That trigger is already gone.");
  });

  it("opens a stored canvas with Called by a workflow as the definition says", () => {
    const stored = {
      schemaVersion: 2,
      kind: CANVAS_KIND,
      triggers: [{ id: "trigger-0000000c", kind: "called" }],
    };
    const plain = canvasFromStored(stored, workflowWith(false), new Set());
    expect(plain.problems).toEqual([]);
    expect(plain.canvas.triggers).toBeUndefined();
    const callable = canvasFromStored(
      { schemaVersion: 2, kind: CANVAS_KIND },
      workflowWith(true),
      new Set(),
    );
    expect(callable.canvas.triggers?.map((intent) => intent.kind)).toEqual([
      "called",
    ]);
    expect(callable.dropped).toBe(false);
  });

  it("reads one input form and one Called by a workflow at most", () => {
    for (const kind of ["manual", "called"])
      expect(
        parseCanvas({
          schemaVersion: 2,
          kind: CANVAS_KIND,
          triggers: [
            { id: "trigger-0000000a", kind },
            { id: "trigger-0000000b", kind },
          ],
        }),
      ).toEqual({
        ok: false,
        problems: [`/triggers/1/kind: use one ${kind} trigger at most.`],
      });
  });

  it("mirrors spec.callable in both directions", () => {
    const synced = syncCalled(
      emptyCanvas(),
      workflowWith(true),
      true,
      ids("0000000a", "0000000b"),
    );
    expect(synced.triggers).toEqual([
      { id: "trigger-0000000a", kind: "manual" },
      { id: "trigger-0000000b", kind: "called" },
    ]);
    expect(syncCalled(synced, workflowWith(true), true)).toBe(synced);
    expect(syncCalled(synced, workflowWith(false), true).triggers).toEqual([
      { id: "trigger-0000000a", kind: "manual" },
    ]);
    expect(syncCalled(emptyCanvas(), workflowWith(false), true)).toEqual(
      emptyCanvas(),
    );
  });
});
