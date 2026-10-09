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
