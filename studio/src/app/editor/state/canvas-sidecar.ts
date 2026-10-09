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
// The canvas sidecar, schema urn:firefly-weave:schema:studio-canvas:v2: what
// Studio keeps beside a workflow's definition. Sticky notes, trigger
// choices, step notes, collapsed frames, the actions the workflow owns with
// what each does, and a viewport. Steps are never placed by hand, so it
// holds no positions: a version 1 layout keeps its viewport and drops them.
// Pure: the model holds one; local drafts and canvas files store it.
import type { Workflow } from "../../model";
import { CANVAS_MAX_BYTES } from "./test-data";

export const CANVAS_SCHEMA_ID =
  "urn:firefly-weave:schema:studio-canvas:v2" as const;
export const CANVAS_KIND = "weave.studio/canvas" as const;
export const NOTE_COLORS = [
  "yellow",
  "blue",
  "green",
  "pink",
  "purple",
  "gray",
] as const;
export type NoteColor = (typeof NOTE_COLORS)[number];
export type TriggerKind =
  | "manual"
  | "webhook"
  | "schedule"
  | "broker"
  | "email"
  | "provider"
  | "called";
export const TRIGGER_KINDS: readonly TriggerKind[] = [
  "manual",
  "webhook",
  "schedule",
  "broker",
  "email",
  "provider",
  "called",
];
export const MAX_NOTES = 200;
export const MAX_NOTE_TEXT = 4000;
export const MAX_STEP_NOTE_TEXT = 2000;
export const MAX_TRIGGERS = 6;
export const MAX_OWNED_ACTIONS = 200;
export const NOTE_WIDTH = { min: 160, max: 1200 } as const;
export const NOTE_HEIGHT = { min: 80, max: 1200 } as const;
export const CANVAS_TOO_BIG =
  "The canvas can hold 256 KB of notes and settings. Shorten or delete a note first.";

export interface NoteAnchor {
  stepId: string;
  dx: number;
  dy: number;
}
export interface StickyNote {
  id: string;
  text: string;
  color: NoteColor;
  x: number;
  y: number;
  width: number;
  height: number;
  /** The step the note follows; x and y keep where it was drawn last. */
  anchor?: NoteAnchor;
}
export interface StepNote {
  text: string;
  showOnCanvas?: boolean;
  [key: string]: unknown;
}
/** A trigger choice; its settings may still be to be chosen (activation needs them). */
export interface TriggerIntent {
  id: string;
  kind: TriggerKind;
  [key: string]: unknown;
}
/** What an owned action does (Studio rebuilds its Action document from it). */
export interface ActionRecipe {
  kind: "http" | "connector";
  [key: string]: unknown;
}
export interface CanvasSidecar {
  schemaVersion: 2;
  kind: typeof CANVAS_KIND;
  semanticDigest?: string;
  viewport?: { x?: number; y?: number; zoom?: number };
  notes?: StickyNote[];
  stepNotes?: Record<string, StepNote>;
  triggers?: TriggerIntent[];
  collapsed?: string[];
  ownedActions?: string[];
  actionRecipes?: Record<string, ActionRecipe>;
}
export type CanvasReading =
  | { ok: true; canvas: CanvasSidecar }
  | { ok: false; problems: string[] };
export interface StoredCanvas {
  canvas: CanvasSidecar;
  /** Why the stored canvas couldn't be read; empty when it could. */
  problems: string[];
  /** Anchors or step notes named steps the workflow doesn't have. */
  dropped: boolean;
}

const NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;
const NOTE_ID = /^note-[0-9a-f]{8}$/;
const TRIGGER_ID = /^trigger-[0-9a-f]{8}$/;
const TOP_FIELDS = new Set([
  "schemaVersion",
  "kind",
  "semanticDigest",
  "viewport",
  "notes",
  "stepNotes",
  "triggers",
  "collapsed",
  "ownedActions",
  "actionRecipes",
]);
const NOTE_FIELDS = new Set([
  "id",
  "text",
  "color",
  "x",
  "y",
  "width",
  "height",
  "anchor",
]);
const NAME_RULE =
  "use letters, numbers, dots, underscores or hyphens, starting with a letter or number.";

const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const isNumber = (value: unknown): value is number =>
  typeof value === "number" && Number.isFinite(value);
const between = (value: unknown, min: number, max: number) =>
  isNumber(value) && value >= min && value <= max;
/** Characters as JSON Schema counts them: code points. */
const characters = (text: string) => [...text].length;
type Problem = (path: string, text: string) => void;

export function emptyCanvas(): CanvasSidecar {
  return { schemaVersion: 2, kind: CANVAS_KIND };
}
/** The bytes the canvas takes as JSON. */
export function canvasBytes(canvas: CanvasSidecar): number {
  return new TextEncoder().encode(JSON.stringify(canvas)).length;
}

function readViewport(value: unknown): CanvasSidecar["viewport"] | null {
  if (!isRecord(value)) return null;
  const viewport: NonNullable<CanvasSidecar["viewport"]> = {};
  for (const key of ["x", "y"] as const) {
    if (!(key in value)) continue;
    if (!isNumber(value[key])) return null;
    viewport[key] = value[key] as number;
  }
  if ("zoom" in value) {
    if (!between(value["zoom"], 0.1, 4)) return null;
    viewport.zoom = value["zoom"] as number;
  }
  return viewport;
}

function readNotes(value: unknown, problem: Problem): StickyNote[] {
  if (!Array.isArray(value)) {
    problem("/notes", "expected a list of notes.");
    return [];
  }
  if (value.length > MAX_NOTES)
    problem("/notes", `keep at most ${MAX_NOTES} notes.`);
  const seen = new Set<string>();
  const notes: StickyNote[] = [];
  value.forEach((item: unknown, index) => {
    const at = `/notes/${index}`;
    if (!isRecord(item)) return problem(at, "expected a note.");
    let fine = true;
    const fail = (field: string, text: string) => {
      problem(`${at}/${field}`, text);
      fine = false;
    };
    for (const key of Object.keys(item))
      if (!NOTE_FIELDS.has(key))
        fail(key, "remove this field; a note doesn't have it.");
    const id = item["id"];
    if (typeof id !== "string" || !NOTE_ID.test(id))
      fail("id", 'use "note-" and 8 lowercase hexadecimal digits.');
    else if (seen.has(id)) fail("id", "use each note ID once.");
    else seen.add(id);
    const text = item["text"];
    if (typeof text !== "string" || characters(text) > MAX_NOTE_TEXT)
      fail("text", `use text of at most ${MAX_NOTE_TEXT} characters.`);
    if (!NOTE_COLORS.includes(item["color"] as NoteColor))
      fail("color", "use yellow, blue, green, pink, purple or gray.");
    if (!isNumber(item["x"])) fail("x", "expected a number.");
    if (!isNumber(item["y"])) fail("y", "expected a number.");
    if (!between(item["width"], NOTE_WIDTH.min, NOTE_WIDTH.max))
      fail("width", `use ${NOTE_WIDTH.min} to ${NOTE_WIDTH.max}.`);
    if (!between(item["height"], NOTE_HEIGHT.min, NOTE_HEIGHT.max))
      fail("height", `use ${NOTE_HEIGHT.min} to ${NOTE_HEIGHT.max}.`);
    const anchor = item["anchor"];
    if (
      anchor !== undefined &&
      !(
        isRecord(anchor) &&
        typeof anchor["stepId"] === "string" &&
        isNumber(anchor["dx"]) &&
        isNumber(anchor["dy"])
      )
    )
      fail("anchor", "expected a stepId and the numbers dx and dy.");
    if (fine) notes.push(structuredClone(item) as unknown as StickyNote);
  });
  return notes;
}

function readStepNotes(
  value: unknown,
  problem: Problem,
): Record<string, StepNote> {
  if (!isRecord(value)) {
    problem("/stepNotes", "expected step notes by step ID.");
    return {};
  }
  const kept: [string, StepNote][] = [];
  for (const [id, note] of Object.entries(value)) {
    const at = `/stepNotes/${id}`;
    if (!isRecord(note) || typeof note["text"] !== "string") {
      problem(at, "expected a note with text.");
      continue;
    }
    if (characters(note["text"]) > MAX_STEP_NOTE_TEXT) {
      problem(
        `${at}/text`,
        `use text of at most ${MAX_STEP_NOTE_TEXT} characters.`,
      );
      continue;
    }
    if ("showOnCanvas" in note && typeof note["showOnCanvas"] !== "boolean") {
      problem(`${at}/showOnCanvas`, "expected true or false.");
      continue;
    }
    kept.push([id, structuredClone(note) as StepNote]);
  }
  return Object.fromEntries(kept);
}

function readTriggers(value: unknown, problem: Problem): TriggerIntent[] {
  if (!Array.isArray(value)) {
    problem("/triggers", "expected a list of triggers.");
    return [];
  }
  if (value.length > MAX_TRIGGERS)
    problem("/triggers", `keep at most ${MAX_TRIGGERS} triggers.`);
  const seen = new Set<string>();
  const triggers: TriggerIntent[] = [];
  value.forEach((item: unknown, index) => {
    const at = `/triggers/${index}`;
    if (!isRecord(item)) return problem(at, "expected a trigger.");
    const id = item["id"];
    let fine = true;
    if (typeof id !== "string" || !TRIGGER_ID.test(id)) {
      problem(`${at}/id`, 'use "trigger-" and 8 lowercase hexadecimal digits.');
      fine = false;
    } else if (seen.has(id)) {
      problem(`${at}/id`, "use each trigger ID once.");
      fine = false;
    } else seen.add(id);
    if (!TRIGGER_KINDS.includes(item["kind"] as TriggerKind)) {
      problem(
        `${at}/kind`,
        "use manual, webhook, schedule, broker, email, provider or called.",
      );
      fine = false;
    }
    if (fine) triggers.push(structuredClone(item) as TriggerIntent);
  });
  return triggers;
}

function readOwned(value: unknown, problem: Problem): string[] {
  if (!Array.isArray(value)) {
    problem("/ownedActions", "expected a list of action names.");
    return [];
  }
  if (value.length > MAX_OWNED_ACTIONS)
    problem("/ownedActions", `keep at most ${MAX_OWNED_ACTIONS} actions.`);
  const seen = new Set<string>();
  value.forEach((name: unknown, index) => {
    if (typeof name !== "string" || !NAME.test(name))
      problem(`/ownedActions/${index}`, NAME_RULE);
    else if (seen.has(name))
      problem(`/ownedActions/${index}`, "list each action once.");
    else seen.add(name);
  });
  return [...seen];
}

function readRecipes(
  value: unknown,
  owned: ReadonlySet<string>,
  problem: Problem,
): Record<string, ActionRecipe> {
  if (!isRecord(value)) {
    problem("/actionRecipes", "expected recipes by action name.");
    return {};
  }
  const kept: [string, ActionRecipe][] = [];
  for (const [name, recipe] of Object.entries(value)) {
    const at = `/actionRecipes/${name}`;
    if (!owned.has(name)) {
      problem(at, "list this action in ownedActions first.");
      continue;
    }
    if (
      !isRecord(recipe) ||
      (recipe["kind"] !== "http" && recipe["kind"] !== "connector")
    ) {
      problem(`${at}/kind`, "use http or connector.");
      continue;
    }
    kept.push([name, structuredClone(recipe) as ActionRecipe]);
  }
  return Object.fromEntries(kept);
}

/** A canvas file as the schema reads it, with every problem as "/path: what to do". */
export function parseCanvas(value: unknown): CanvasReading {
  const problems: string[] = [];
  const problem: Problem = (path, text) => {
    problems.push(`${path}: ${text}`);
  };
  if (!isRecord(value))
    return { ok: false, problems: ["/: expected a canvas object."] };
  for (const key of Object.keys(value))
    if (!TOP_FIELDS.has(key))
      problem(`/${key}`, "remove this field; a canvas file doesn't have it.");
  if (value["schemaVersion"] !== 2) problem("/schemaVersion", "expected 2.");
  if (value["kind"] !== CANVAS_KIND)
    problem("/kind", `expected "${CANVAS_KIND}".`);
  const canvas = emptyCanvas();
  if ("semanticDigest" in value) {
    if (typeof value["semanticDigest"] === "string")
      canvas.semanticDigest = value["semanticDigest"];
    else problem("/semanticDigest", "expected text.");
  }
  if ("viewport" in value) {
    const viewport = readViewport(value["viewport"]);
    if (viewport) canvas.viewport = viewport;
    else
      problem(
        "/viewport",
        "expected the numbers x and y, and a zoom from 0.1 to 4.",
      );
  }
  if ("notes" in value) canvas.notes = readNotes(value["notes"], problem);
  if ("stepNotes" in value)
    canvas.stepNotes = readStepNotes(value["stepNotes"], problem);
  if ("triggers" in value)
    canvas.triggers = readTriggers(value["triggers"], problem);
  if ("collapsed" in value) {
    const collapsed = value["collapsed"];
    if (
      Array.isArray(collapsed) &&
      collapsed.every((item: unknown) => typeof item === "string")
    )
      canvas.collapsed = [...(collapsed as string[])];
    else problem("/collapsed", "expected a list of step IDs.");
  }
  if ("ownedActions" in value)
    canvas.ownedActions = readOwned(value["ownedActions"], problem);
  if ("actionRecipes" in value)
    canvas.actionRecipes = readRecipes(
      value["actionRecipes"],
      new Set(canvas.ownedActions ?? []),
      problem,
    );
  if (!problems.length && canvasBytes(canvas) > CANVAS_MAX_BYTES)
    problem("/", "keep the canvas file under 256 KB.");
  return problems.length ? { ok: false, problems } : { ok: true, canvas };
}

/**
 * Any canvas Studio stored: a version 2 canvas, a version 1 layout file or
 * the layout a local draft kept before (its viewport stays, positions go),
 * or nothing at all.
 */
export function readCanvas(value: unknown): CanvasReading {
  if (value === null || value === undefined)
    return { ok: true, canvas: emptyCanvas() };
  if (isRecord(value) && value["schemaVersion"] === 2)
    return parseCanvas(value);
  const layout =
    isRecord(value) &&
    (value["schemaVersion"] === 1 ||
      (!("schemaVersion" in value) && isRecord(value["positions"])));
  if (!layout || !isRecord(value))
    return { ok: false, problems: ["/: this isn't a Studio canvas file."] };
  const canvas = emptyCanvas();
  if (typeof value["semanticDigest"] === "string")
    canvas.semanticDigest = value["semanticDigest"];
  const viewport = readViewport(value["viewport"]);
  if (viewport && "x" in viewport && "y" in viewport && "zoom" in viewport)
    canvas.viewport = viewport;
  return { ok: true, canvas };
}

/** A renamed step keeps its notes: anchors and step notes follow the new ID. */
export function renameInCanvas(
  canvas: CanvasSidecar,
  from: string,
  to: string,
): CanvasSidecar {
  let changed = false;
  const next = { ...canvas };
  if (canvas.notes?.some((note) => note.anchor?.stepId === from)) {
    changed = true;
    next.notes = canvas.notes.map((note) =>
      note.anchor?.stepId === from
        ? { ...note, anchor: { ...note.anchor, stepId: to } }
        : note,
    );
  }
  if (canvas.stepNotes && Object.hasOwn(canvas.stepNotes, from)) {
    changed = true;
    next.stepNotes = Object.fromEntries(
      Object.entries(canvas.stepNotes).map(([id, note]) => [
        id === from ? to : id,
        note,
      ]),
    );
  }
  return changed ? next : canvas;
}

/**
 * Steps that are gone: notes anchored to them stay where they were drawn
 * last, without the anchor; their step notes go. The notes of trigger
 * choices and of the workflow's own start and end (`$trigger`, `$end`) stay.
 */
export function pruneCanvas(
  canvas: CanvasSidecar,
  present: ReadonlySet<string>,
): CanvasSidecar {
  const triggers = new Set((canvas.triggers ?? []).map((intent) => intent.id));
  const keeps = (id: string) =>
    present.has(id) || triggers.has(id) || id.startsWith("$");
  let changed = false;
  const next = { ...canvas };
  if (
    canvas.notes?.some(
      (note) => note.anchor && !present.has(note.anchor.stepId),
    )
  ) {
    changed = true;
    next.notes = canvas.notes.map((note) => {
      if (!note.anchor || present.has(note.anchor.stepId)) return note;
      const free = { ...note };
      delete free.anchor;
      return free;
    });
  }
  if (
    canvas.stepNotes &&
    Object.keys(canvas.stepNotes).some((id) => !keeps(id))
  ) {
    changed = true;
    const kept = Object.entries(canvas.stepNotes).filter(([id]) => keeps(id));
    if (kept.length) next.stepNotes = Object.fromEntries(kept);
    else delete next.stepNotes;
  }
  return changed ? next : canvas;
}

/** A stored canvas for the workflow it opens with. */
export function canvasFromStored(
  stored: unknown,
  workflow: Workflow,
  present: ReadonlySet<string>,
): StoredCanvas {
  // The workflow is in the signature for trigger choices that follow the definition (Called by a workflow).
  void workflow;
  const read = readCanvas(stored);
  const base = read.ok ? read.canvas : emptyCanvas();
  const canvas = pruneCanvas(base, present);
  return {
    canvas,
    problems: read.ok ? [] : read.problems,
    dropped: canvas !== base,
  };
}

/** The canvas file Save to file writes next to the workflow. */
export function canvasFile(
  canvas: CanvasSidecar,
  semanticDigest: string,
): string {
  return `${JSON.stringify({ ...canvas, semanticDigest }, null, 2)}\n`;
}

// ------------------------------------------------------------ step notes
export const stepNotesOf = (canvas: CanvasSidecar): Record<string, StepNote> =>
  canvas.stepNotes ?? {};

/** The canvas with a step's (or trigger's) note set; null or blank text removes it. */
export function withStepNote(
  canvas: CanvasSidecar,
  id: string,
  note: StepNote | null,
): CanvasSidecar {
  const notes = { ...stepNotesOf(canvas) };
  if (note && note.text.trim()) notes[id] = { ...note };
  else if (id in notes) delete notes[id];
  else return canvas;
  const next = { ...canvas };
  if (Object.keys(notes).length) next.stepNotes = notes;
  else delete next.stepNotes;
  return next;
}

/** The note's first line when it shows on the canvas, else "". */
export function stepNoteLine(canvas: CanvasSidecar, id: string): string {
  const note = stepNotesOf(canvas)[id];
  return note?.showOnCanvas
    ? (note.text.split("\n").find((line) => line.trim()) ?? "").trim()
    : "";
}

// --------------------------------------------------------- owned actions
export const ownedRecipes = (
  canvas: CanvasSidecar,
): Record<string, ActionRecipe> => canvas.actionRecipes ?? {};

/** The canvas with an owned action's recipe set (and its name listed), or both removed (null). */
export function withOwnedRecipe(
  canvas: CanvasSidecar,
  name: string,
  recipe: ActionRecipe | null,
): CanvasSidecar {
  const names = (canvas.ownedActions ?? []).filter((item) => item !== name);
  const recipes = { ...ownedRecipes(canvas) };
  delete recipes[name];
  if (recipe) {
    names.push(name);
    recipes[name] = structuredClone(recipe);
  }
  const next = { ...canvas };
  if (names.length) next.ownedActions = names;
  else delete next.ownedActions;
  if (Object.keys(recipes).length) next.actionRecipes = recipes;
  else delete next.actionRecipes;
  return next;
}

/** An owned action renamed: its name and recipe move together, in place. */
export function renameOwnedRecipe(
  canvas: CanvasSidecar,
  from: string,
  to: string,
): CanvasSidecar {
  const recipe = ownedRecipes(canvas)[from];
  if (!recipe || from === to) return canvas;
  return {
    ...canvas,
    ownedActions: (canvas.ownedActions ?? []).map((name) =>
      name === from ? to : name,
    ),
    actionRecipes: Object.fromEntries(
      Object.entries(ownedRecipes(canvas)).map(([name, r]) => [
        name === from ? to : name,
        r,
      ]),
    ),
  };
}

/** The canvas without the owned actions no step uses any more. */
export function pruneOwnedRecipes(
  canvas: CanvasSidecar,
  used: ReadonlySet<string>,
): CanvasSidecar {
  let next = canvas;
  for (const name of canvas.ownedActions ?? [])
    if (!used.has(name)) next = withOwnedRecipe(next, name, null);
  return next;
}

// -------------------------------------------------------- trigger choices
export const triggerIntents = (canvas: CanvasSidecar): TriggerIntent[] =>
  canvas.triggers ?? [];

/** The canvas with these trigger choices (at most six); an empty list leaves the field out. */
export function withTriggerIntents(
  canvas: CanvasSidecar,
  triggers: TriggerIntent[],
): CanvasSidecar {
  const next = { ...canvas };
  if (triggers.length)
    next.triggers = triggers
      .slice(0, MAX_TRIGGERS)
      .map((intent) => ({ ...intent }));
  else delete next.triggers;
  return next;
}
