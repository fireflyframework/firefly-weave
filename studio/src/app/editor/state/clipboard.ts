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
// Copy and paste of steps as YAML. Copy writes the selected run of steps
// with the connection slots and AI profiles they use, their pinned outputs
// (never ones recorded from a run or a test call) and the recipes of the
// actions the workflow owns for them. Paste reads that document, a list of
// steps, one step or a whole workflow; it renumbers taken IDs, follows
// references between the pasted steps, makes the pasted steps' own actions
// the new workflow's own, and finds the references the new place can't
// read. Pure: the shell applies the plan.
//
// The clipboard is untrusted text: reading it refuses more than 1 MiB, YAML
// aliases, keys that reach an object's prototype, values nested deeper than
// the canvas reads and any field the workflow language or an action recipe
// doesn't have. Copying writes only the fields a recipe has, so nothing a
// hand-edited canvas file carries beyond them reaches the clipboard.
import { parseDocument, stringify } from "yaml";
import { canonicalJson, escapeSegment } from "../../forms/core/json";
import { visibleSteps } from "../../forms/core/scope";
import {
  kinds,
  rewriteStepRefs,
  sequencesOf,
  stepIdPattern,
  stepIdPrefix,
  type Kind,
  type Step,
  type Workflow,
} from "../../model";
import type { Insertion } from "../canvas/layout-ltr";
import {
  nameOfUses,
  ownedName,
  ownedUses,
  type OwnedRecipe,
} from "../ndv/owned/owned-actions";
import { ownedActionsOf, unpublished } from "../ndv/owned/owned-store";
import { emptyCanvas, type CanvasSidecar } from "./canvas-sidecar";
import type { PinEntry } from "./test-data";

export const CLIPBOARD_HEADER = "# Firefly Weave steps. Paste into Studio.";
export const CLIPBOARD_WEB_FORMAT = "web application/x-weave-steps";
export const CLIPBOARD_MAX_BYTES = 1_048_576;
export const NOT_STEPS = "The clipboard doesn't hold Weave steps.";

export interface ClipboardDocument {
  weaveClipboard: 1;
  steps: Step[];
  connections: Record<string, unknown>;
  llmProfiles: Record<string, unknown>;
  pins: Record<string, PinEntry>;
  /** What the actions the steps' workflow owned for them do, by `uses`; only when there are some. */
  actions?: Record<string, OwnedRecipe>;
}
export interface Copied {
  document: ClipboardDocument;
  /** For text/plain: the document as YAML under a comment. */
  yaml: string;
  /** For the web custom format. */
  json: string;
}
export interface ClipboardContent {
  steps: Step[];
  connections: Record<string, unknown>;
  llmProfiles: Record<string, unknown>;
  pins: Record<string, unknown>;
  actions: Record<string, unknown>;
}
export type ClipboardReading =
  | { ok: true; content: ClipboardContent }
  | { ok: false; reason: string };
/** A reference a pasted step holds that its new place can't read. */
export interface Unreadable {
  stepId: string;
  ref: string;
  /** Where the reference sits in the step. */
  path: (string | number)[];
  /** The same place as a pointer, for the scope rules. */
  fieldPath: string;
  /** The step it reads. */
  target: string;
  /** "later": the step runs later or on another path; "missing": it isn't in the workflow. */
  reason: "later" | "missing";
}
/** The workflow a paste lands in, beyond its definition. */
export interface PasteTarget {
  canvas: CanvasSidecar;
  /** Action names other documents already use (published or shared). */
  reserved: ReadonlySet<string>;
}
/** An action the paste makes the workflow's own. */
export interface OwnedAdd {
  uses: string;
  recipe: OwnedRecipe;
}
export interface PastePlan {
  at: Insertion;
  steps: Step[];
  /** The pasted steps' IDs after renumbering, in order. */
  ids: string[];
  /** Old ID to new ID, for steps whose ID was taken. */
  renamed: Record<string, string>;
  /** Connection slots and AI profiles to declare, by name. */
  addConnections: Record<string, unknown>;
  addProfiles: Record<string, unknown>;
  renamedSlots: Record<string, string>;
  renamedProfiles: Record<string, string>;
  pins: Record<string, unknown>;
  ownedAdds: OwnedAdd[];
  unreadable: Unreadable[];
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const runDerived = (entry: unknown) =>
  isRecord(entry) &&
  (entry["source"] === "run" || entry["source"] === "test-call");
const holder = (steps: Step[]): Workflow => ({
  apiVersion: "",
  kind: "Workflow",
  metadata: { name: "", version: "" },
  spec: { steps },
});
/** The most a sentence repeats of what it refuses. */
const ECHO_MAX = 60;
/** Text-direction and other invisible format controls, which could reorder a sentence. */
const FORMAT_CONTROLS =
  /[\u061C\u200B-\u200F\u202A-\u202E\u2060-\u2069\uFEFF]/g;
/**
 * Clipboard text as a sentence shows it: without format controls, and cut
 * to 60 characters with an ellipsis, since the clipboard can hold anything.
 */
export function shown(value: unknown): string {
  const text = String(value).replace(FORMAT_CONTROLS, "");
  const characters = [...text];
  return characters.length > ECHO_MAX
    ? `${characters.slice(0, ECHO_MAX - 1).join("")}…`
    : text;
}
const PATHLESS = (id: unknown) =>
  `The pasted step ${shown(id)} has a path without a list of steps.`;
const UNKNOWN_FIELD = (id: unknown, key: string) =>
  `The pasted step ${shown(id)} has a field Studio doesn't know: ${shown(key)}.`;
const UNREADABLE_ACTION = (uses: string) =>
  `The pasted steps bring an action Studio can't read: ${shown(uses)}.`;
const UNKNOWN_KIND = (kind: string) =>
  `This paste has a step kind Studio doesn't know: ${shown(kind)}.`;
/** The most steps one paste brings, nested ones counted. */
export const PASTE_MAX_STEPS = 500;
/** The longest step, slot or profile name Studio keeps, as elsewhere in Studio. */
export const NAME_MAX = 128;
/**
 * The fields each step kind has in the workflow language, which refuses any
 * other; loops and workflow calls too, for platforms whose language has them.
 */
const STEP_FIELDS: Record<string, ReadonlySet<string>> = {
  action: new Set(["id", "kind", "uses", "with", "connection"]),
  decisionTable: new Set(["id", "kind", "uses", "with"]),
  llm: new Set([
    "id",
    "kind",
    "uses",
    "profile",
    "prompt",
    "context",
    "connection",
  ]),
  transform: new Set(["id", "kind", "value"]),
  switch: new Set(["id", "kind", "cases", "default"]),
  parallel: new Set(["id", "kind", "branches", "concurrency"]),
  wait: new Set(["id", "kind", "durationSeconds"]),
  signal: new Set(["id", "kind", "name", "timeoutSeconds", "payloadSchema"]),
  humanTask: new Set([
    "id",
    "kind",
    "assignment",
    "title",
    "context",
    "formSchema",
    "decisions",
    "dueSeconds",
    "expirySeconds",
  ]),
  fail: new Set(["id", "kind", "code", "message"]),
  forEach: new Set([
    "id",
    "kind",
    "items",
    "concurrency",
    "maxItems",
    "collect",
    "body",
  ]),
  callWorkflow: new Set([
    "id",
    "kind",
    "uses",
    "with",
    "mode",
    "onFailure",
    "businessKey",
  ]),
};
const PATH_FIELDS = new Set(["steps", "output"]);
const CASE_FIELDS = new Set(["steps", "output", "when"]);
const DOCUMENT_FIELDS = new Set([
  "weaveClipboard",
  "steps",
  "connections",
  "llmProfiles",
  "pins",
  "actions",
]);
const SLOT_FIELDS = new Set(["connector", "required"]);
/** Keys that would reach an object's prototype if code ever assigned them. */
const PROTOTYPE_KEYS = new Set(["__proto__", "constructor", "prototype"]);
/** Deeper than this, a pasted value is refused: the canvas and the forms read far less. */
const MAX_DEPTH = 64;
const HTTP_METHODS = new Set(["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]);
const RECIPE_FIELDS = {
  http: new Set([
    "kind",
    "method",
    "pathTemplate",
    "responseSample",
    "statuses",
    "description",
    "timeoutSeconds",
    "retry",
    "version",
    "published",
  ]),
  connector: new Set([
    "kind",
    "connector",
    "action",
    "timeoutSeconds",
    "retry",
    "description",
    "version",
    "published",
  ]),
} as const;
const RETRY_FIELDS = [
  "maxAttempts",
  "initialDelaySeconds",
  "maxDelaySeconds",
] as const;
const CONNECTOR_REF = /^[a-z0-9][a-z0-9-]{0,127}@\d+\.\d+\.\d+$/;
const ACTION_NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;
const USES = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}@\d+\.\d+\.\d+$/;
/** The language's names (ResourceName), within Studio's 128 characters. */
const resourceName = (name: string) =>
  name.length <= NAME_MAX && /^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(name);
const SEMVER =
  "(?:0|[1-9][0-9]*)\\.(?:0|[1-9][0-9]*)\\.(?:0|[1-9][0-9]*)" +
  "(?:-(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)(?:\\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*)?" +
  "(?:\\+[0-9A-Za-z-]+(?:\\.[0-9A-Za-z-]+)*)?";
/** A connector reference as the language reads it (VersionedReference). */
const VERSIONED = new RegExp(
  `^[A-Za-z0-9][A-Za-z0-9_.-]{0,${NAME_MAX - 1}}@${SEMVER}$`,
);

/**
 * True when a parsed value is safe to keep: no key reaches a prototype and
 * nothing nests deeper than MAX_DEPTH. Walks with its own stack, so a deep
 * value can't overflow the call stack.
 */
function safeShape(value: unknown): boolean {
  const stack: [unknown, number][] = [[value, 0]];
  while (stack.length) {
    const [current, depth] = stack.pop()!;
    if (!current || typeof current !== "object") continue;
    if (depth > MAX_DEPTH) return false;
    if (Array.isArray(current)) {
      for (const item of current) stack.push([item, depth + 1]);
      continue;
    }
    for (const key of Object.keys(current)) {
      if (PROTOTYPE_KEYS.has(key)) return false;
      stack.push([(current as Record<string, unknown>)[key], depth + 1]);
    }
    if (Object.getPrototypeOf(current) !== Object.prototype) return false;
  }
  return true;
}
const unknownKey = (
  value: Record<string, unknown>,
  known: ReadonlySet<string>,
) => Object.keys(value).find((key) => !known.has(key)) ?? null;
const positiveInteger = (value: unknown) =>
  typeof value === "number" && Number.isSafeInteger(value) && value > 0;

/** A step and every step inside its paths and branches. */
export function stepTree(step: Step): Step[] {
  return [...sequencesOf(holder([step])).values()].flat();
}

/** The step an instance key belongs to: `send[2]` and `support#2.1` belong to send and support. */
export function instanceStep(key: string): string {
  return /^[A-Za-z0-9][A-Za-z0-9_.-]*/.exec(key)?.[0] ?? key;
}

export function copySteps(
  workflow: Workflow,
  ids: readonly string[],
  pins: Readonly<Record<string, PinEntry>> = {},
  owned: Readonly<Record<string, OwnedRecipe>> = {},
): Copied {
  const sequences = [...sequencesOf(workflow).values()];
  const steps = ids.flatMap((id) => {
    for (const list of sequences) {
      const step = list.find((item) => item.id === id);
      if (step) return [structuredClone(step)];
    }
    return [];
  });
  const tree = steps.flatMap(stepTree);
  const slots = isRecord(workflow.spec["connections"])
    ? workflow.spec["connections"]
    : {};
  const profiles = isRecord(workflow.spec["llmProfiles"])
    ? workflow.spec["llmProfiles"]
    : {};
  const connections: [string, unknown][] = [];
  const llmProfiles: [string, unknown][] = [];
  const actions: [string, OwnedRecipe][] = [];
  for (const step of tree) {
    const slot = step["connection"];
    if (typeof slot === "string" && Object.hasOwn(slots, slot))
      connections.push([slot, structuredClone(slots[slot])]);
    const profile = step["profile"];
    if (
      step.kind === "llm" &&
      typeof profile === "string" &&
      Object.hasOwn(profiles, profile)
    )
      llmProfiles.push([profile, structuredClone(profiles[profile])]);
    const uses = step["uses"];
    const recipe =
      step.kind === "action" &&
      typeof uses === "string" &&
      Object.hasOwn(owned, uses)
        ? recipeOf(recipeFields(owned[uses]))
        : null;
    if (recipe) actions.push([uses as string, unpublished(recipe)]);
  }
  const inTree = new Set(tree.map((step) => step.id));
  const document: ClipboardDocument = {
    weaveClipboard: 1,
    steps,
    connections: Object.fromEntries(connections),
    llmProfiles: Object.fromEntries(llmProfiles),
    pins: Object.fromEntries(
      Object.entries(pins)
        .filter(
          ([key, entry]) => inTree.has(instanceStep(key)) && !runDerived(entry),
        )
        .map(([key, entry]) => [key, structuredClone(entry)]),
    ),
    ...(actions.length ? { actions: Object.fromEntries(actions) } : {}),
  };
  return {
    document,
    yaml: `${CLIPBOARD_HEADER}\n${stringify(document)}`,
    json: JSON.stringify(document),
  };
}

function checkStep(
  step: unknown,
  known: ReadonlySet<string>,
  depth: number,
): string | null {
  if (
    !isRecord(step) ||
    typeof step["id"] !== "string" ||
    typeof step["kind"] !== "string"
  )
    return NOT_STEPS;
  if (!known.has(step["kind"])) return UNKNOWN_KIND(step["kind"]);
  const fields = STEP_FIELDS[step["kind"]];
  if (!fields) return UNKNOWN_KIND(step["kind"]);
  const unknown = unknownKey(step, fields);
  if (unknown) return UNKNOWN_FIELD(step["id"], unknown);
  if (depth > 40)
    return "The pasted steps nest too deeply for the canvas. Paste them in Source.";
  const lanes: unknown[] = [];
  if (step["kind"] === "switch") {
    if (!Array.isArray(step["cases"]) || !isRecord(step["default"]))
      return PATHLESS(step["id"]);
    lanes.push(...step["cases"], step["default"]);
  } else if (step["kind"] === "parallel") {
    if (!isRecord(step["branches"])) return PATHLESS(step["id"]);
    lanes.push(...Object.values(step["branches"]));
  } else if (step["kind"] === "forEach") {
    if (!isRecord(step["body"])) return PATHLESS(step["id"]);
    lanes.push(step["body"]);
  }
  for (const lane of lanes) {
    if (!isRecord(lane) || !Array.isArray(lane["steps"]))
      return PATHLESS(step["id"]);
    const field = unknownKey(
      lane,
      step["kind"] === "switch" && lane !== step["default"]
        ? CASE_FIELDS
        : PATH_FIELDS,
    );
    if (field) return UNKNOWN_FIELD(step["id"], field);
    for (const inner of lane["steps"]) {
      const problem = checkStep(inner, known, depth + 1);
      if (problem) return problem;
    }
  }
  return null;
}

/** Text from the clipboard as steps, or why it isn't. */
export function readClipboard(
  text: string,
  known: ReadonlySet<string> = new Set(kinds),
): ClipboardReading {
  if (new TextEncoder().encode(text).length > CLIPBOARD_MAX_BYTES)
    return {
      ok: false,
      reason: "The clipboard holds more than 1 MiB. Copy fewer steps.",
    };
  let value: unknown;
  try {
    const document = parseDocument(text, {
      uniqueKeys: true,
      maxAliasCount: 0,
    } as never);
    if (document.errors.length) return { ok: false, reason: NOT_STEPS };
    value = document.toJS({ maxAliasCount: 0 });
    if (!safeShape(value)) return { ok: false, reason: NOT_STEPS };
  } catch (error) {
    return {
      ok: false,
      reason: /alias/i.test(String(error))
        ? "Studio doesn't paste YAML aliases (*name). Copy the steps without them."
        : NOT_STEPS,
    };
  }
  let found: {
    steps: unknown[];
    connections?: unknown;
    llmProfiles?: unknown;
    pins?: unknown;
    actions?: unknown;
  } | null = null;
  if (
    isRecord(value) &&
    value["weaveClipboard"] === 1 &&
    Array.isArray(value["steps"])
  ) {
    const field = unknownKey(value, DOCUMENT_FIELDS);
    if (field)
      return {
        ok: false,
        reason: `This paste has a field Studio doesn't know: ${shown(field)}.`,
      };
    found = {
      steps: value["steps"],
      connections: value["connections"],
      llmProfiles: value["llmProfiles"],
      pins: value["pins"],
      actions: value["actions"],
    };
  } else if (Array.isArray(value)) found = { steps: value };
  else if (
    isRecord(value) &&
    value["kind"] === "Workflow" &&
    isRecord(value["spec"]) &&
    Array.isArray(value["spec"]["steps"])
  )
    found = {
      steps: value["spec"]["steps"],
      connections: value["spec"]["connections"],
      llmProfiles: value["spec"]["llmProfiles"],
    };
  else if (
    isRecord(value) &&
    typeof value["id"] === "string" &&
    typeof value["kind"] === "string"
  )
    found = { steps: [value] };
  if (!found) return { ok: false, reason: NOT_STEPS };
  if (!found.steps.length)
    return { ok: false, reason: "The clipboard holds no steps." };
  for (const step of found.steps) {
    const problem = checkStep(step, known, 0);
    if (problem) return { ok: false, reason: problem };
  }
  if ((found.steps as Step[]).flatMap(stepTree).length > PASTE_MAX_STEPS)
    return {
      ok: false,
      reason: `Studio pastes at most ${PASTE_MAX_STEPS} steps at a time. Copy fewer steps.`,
    };
  if (found.connections !== undefined && !isRecord(found.connections))
    return { ok: false, reason: NOT_STEPS };
  for (const [name, slot] of Object.entries(found.connections ?? {}))
    if (
      !resourceName(name) ||
      !isRecord(slot) ||
      unknownKey(slot, SLOT_FIELDS) ||
      typeof slot["connector"] !== "string" ||
      !VERSIONED.test(slot["connector"]) ||
      (slot["required"] !== undefined && typeof slot["required"] !== "boolean")
    )
      return {
        ok: false,
        reason: `This paste has a connection slot Studio can't read: ${shown(name)}.`,
      };
  if (found.llmProfiles !== undefined && !isRecord(found.llmProfiles))
    return { ok: false, reason: NOT_STEPS };
  for (const [name, profile] of Object.entries(found.llmProfiles ?? {}))
    if (!resourceName(name) || !isRecord(profile))
      return {
        ok: false,
        reason: `This paste has an AI profile Studio can't read: ${shown(name)}.`,
      };
  const actions: Record<string, OwnedRecipe> = {};
  if (found.actions !== undefined && !isRecord(found.actions))
    return { ok: false, reason: NOT_STEPS };
  for (const [uses, value] of Object.entries(found.actions ?? {})) {
    const recipe = USES.test(uses) ? recipeOf(value) : null;
    if (!recipe) return { ok: false, reason: UNREADABLE_ACTION(uses) };
    actions[uses] = recipe;
  }
  return {
    ok: true,
    content: {
      steps: found.steps as Step[],
      connections: isRecord(found.connections) ? found.connections : {},
      llmProfiles: isRecord(found.llmProfiles) ? found.llmProfiles : {},
      pins: isRecord(found.pins) ? found.pins : {},
      actions,
    },
  };
}

/** Every `{ref}` a step holds outside its paths, with where it sits. */
function refSites(
  step: Step,
): { ref: string; path: (string | number)[]; fieldPath: string }[] {
  const sites: { ref: string; path: (string | number)[]; fieldPath: string }[] =
    [];
  const nested = (path: readonly (string | number)[]) =>
    (step.kind === "switch" &&
      ((path[0] === "cases" && path.length === 3 && path[2] === "steps") ||
        (path[0] === "default" && path.length === 2 && path[1] === "steps"))) ||
    (step.kind === "parallel" &&
      path[0] === "branches" &&
      path.length === 3 &&
      path[2] === "steps");
  const walk = (value: unknown, path: (string | number)[]) => {
    if (nested(path)) return;
    if (Array.isArray(value)) {
      value.forEach((item, index) => walk(item, [...path, index]));
      return;
    }
    if (!isRecord(value)) return;
    const keys = Object.keys(value);
    const ref = value["ref"];
    if (keys.length === 1 && typeof ref === "string") {
      sites.push({
        ref,
        path,
        fieldPath: `/${path.map((segment) => escapeSegment(String(segment))).join("/")}`,
      });
      return;
    }
    for (const key of keys)
      if (key !== "literal") walk(value[key], [...path, key]);
  };
  for (const [key, value] of Object.entries(step))
    if (key !== "id" && key !== "kind") walk(value, [key]);
  return sites;
}

/** Names the clipboard brings that the workflow lacks; a name another value holds becomes `<name>-2`. */
function brought(
  theirs: Record<string, unknown>,
  ours: Record<string, unknown>,
): { add: Record<string, unknown>; rename: Record<string, string> } {
  const add: [string, unknown][] = [];
  const rename: [string, string][] = [];
  const names = new Set(Object.keys(ours));
  for (const [name, value] of Object.entries(theirs)) {
    if (!Object.hasOwn(ours, name)) {
      names.add(name);
      add.push([name, value]);
      continue;
    }
    if (canonicalJson(ours[name]) === canonicalJson(value)) continue;
    let i = 2;
    while (names.has(`${name}-${i}`) || Object.hasOwn(theirs, `${name}-${i}`))
      i++;
    const next = `${name}-${i}`;
    names.add(next);
    rename.push([name, next]);
    add.push([next, value]);
  }
  return { add: Object.fromEntries(add), rename: Object.fromEntries(rename) };
}

/**
 * A recipe from the clipboard or a canvas, with only the fields a recipe
 * has, when it has the shape Studio writes; else null.
 */
function recipeOf(value: unknown): OwnedRecipe | null {
  if (!isRecord(value)) return null;
  const kind = value["kind"];
  if (kind !== "http" && kind !== "connector") return null;
  if (unknownKey(value, RECIPE_FIELDS[kind])) return null;
  const optional = (key: string, check: (item: unknown) => boolean) =>
    value[key] === undefined || check(value[key]);
  const common =
    optional("timeoutSeconds", positiveInteger) &&
    optional(
      "description",
      (item) => typeof item === "string" && item.length <= 2048,
    ) &&
    optional(
      "retry",
      (item) =>
        isRecord(item) &&
        !unknownKey(item, new Set(RETRY_FIELDS)) &&
        RETRY_FIELDS.every((key) => positiveInteger(item[key])),
    ) &&
    optional(
      "version",
      (item) => typeof item === "string" && /^\d+\.\d+\.\d+$/.test(item),
    ) &&
    optional(
      "published",
      (item) =>
        isRecord(item) &&
        !unknownKey(item, new Set(["version", "input"])) &&
        typeof item["version"] === "string" &&
        typeof item["input"] === "string",
    );
  if (!common) return null;
  if (kind === "http") {
    const statuses = value["statuses"];
    if (
      typeof value["method"] !== "string" ||
      !HTTP_METHODS.has(value["method"]) ||
      typeof value["pathTemplate"] !== "string" ||
      value["pathTemplate"].length > 2048 ||
      !Array.isArray(statuses) ||
      statuses.length > 100 ||
      !statuses.every(
        (code) => Number.isInteger(code) && code >= 100 && code <= 599,
      ) ||
      !positiveInteger(value["timeoutSeconds"])
    )
      return null;
  } else if (
    typeof value["connector"] !== "string" ||
    !CONNECTOR_REF.test(value["connector"]) ||
    typeof value["action"] !== "string" ||
    !ACTION_NAME.test(value["action"])
  )
    return null;
  return structuredClone(value) as unknown as OwnedRecipe;
}
/** Only the fields a recipe has: what a canvas file carries beyond them stays off the clipboard. */
function recipeFields(value: unknown): unknown {
  if (
    !isRecord(value) ||
    (value["kind"] !== "http" && value["kind"] !== "connector")
  )
    return value;
  const known = RECIPE_FIELDS[value["kind"]];
  return Object.fromEntries(
    Object.entries(value).filter(([key]) => known.has(key)),
  );
}
const sameOperation = (a: OwnedRecipe, b: OwnedRecipe) =>
  a.kind === "connector" &&
  b.kind === "connector" &&
  a.connector === b.connector &&
  a.action === b.action;

/** Pasted steps whose actions came with them call this workflow's own actions instead. */
function ownActions(
  workflow: Workflow,
  steps: readonly Step[],
  actions: Record<string, unknown>,
  target: PasteTarget,
): OwnedAdd[] {
  const owned = ownedActionsOf(target.canvas);
  const taken = new Set<string>([
    ...Object.keys(owned).map(nameOfUses),
    ...[...sequencesOf(workflow).values()]
      .flat()
      .map((step) => step["uses"])
      .filter((uses): uses is string => typeof uses === "string" && !!uses)
      .map(nameOfUses),
    ...target.reserved,
  ]);
  const adds: OwnedAdd[] = [];
  for (const step of steps.flatMap(stepTree)) {
    const uses = step["uses"];
    if (step.kind !== "action" || typeof uses !== "string") continue;
    const recipe = recipeOf(actions[uses]);
    if (!recipe) continue;
    if (recipe.kind === "connector") {
      const shared =
        Object.entries(owned).find(([, existing]) =>
          sameOperation(existing, recipe),
        )?.[0] ?? adds.find((add) => sameOperation(add.recipe, recipe))?.uses;
      if (shared) {
        step["uses"] = shared;
        continue;
      }
    }
    const name = nameOfUses(uses);
    const suffix =
      recipe.kind === "http"
        ? step.id
        : name.slice(name.indexOf(".") + 1) || step.id;
    const next = ownedName(workflow.metadata.name, suffix, taken);
    taken.add(next);
    const nextUses = ownedUses(next);
    adds.push({ uses: nextUses, recipe: unpublished(recipe) });
    step["uses"] = nextUses;
  }
  return adds;
}

export function planPaste(
  workflow: Workflow,
  content: ClipboardContent,
  at: Insertion,
  taken: ReadonlySet<string>,
  target: PasteTarget = { canvas: emptyCanvas(), reserved: new Set() },
): PastePlan | { reason: string } {
  const list = sequencesOf(workflow).get(at.owner);
  if (!list || at.index < 0 || at.index > list.length)
    return {
      reason:
        "That place in the workflow changed. Choose where to paste again.",
    };
  // 1. IDs: a free one stays; a taken one takes the next free ID on its base.
  const pasted = structuredClone(content.steps);
  const used = new Set(taken);
  const kept = new Set<string>();
  const renamed = new Map<string, string>();
  // Where each base's numbering goes on: the next number never looks back.
  const counters = new Map<string, number>();
  const nextFree = (base: string) => {
    let i = counters.get(base) ?? 1;
    while (used.has(`${base}-${i}`)) i++;
    counters.set(base, i + 1);
    return `${base}-${i}`;
  };
  for (const step of pasted.flatMap(stepTree)) {
    const old = step.id;
    const valid = old.length <= NAME_MAX && stepIdPattern.test(old);
    if (valid && !used.has(old)) {
      used.add(old);
      kept.add(old);
      continue;
    }
    // Room for "-<number>" within the 128 characters.
    const base = valid
      ? (old.replace(/-\d+$/, "") || old).slice(0, NAME_MAX - 8)
      : (stepIdPrefix[step.kind as Kind] ?? "step");
    const next = nextFree(base);
    used.add(next);
    if (!renamed.has(old) && !kept.has(old)) renamed.set(old, next);
    step.id = next;
  }
  // 2. References between the pasted steps follow renamed ones.
  const steps = pasted.map((step) => rewriteStepRefs(step, renamed) as Step);
  // 3. The slots, AI profiles and owned actions the steps bring.
  const slots = isRecord(workflow.spec["connections"])
    ? workflow.spec["connections"]
    : {};
  const profiles = isRecord(workflow.spec["llmProfiles"])
    ? workflow.spec["llmProfiles"]
    : {};
  const connections = brought(content.connections, slots);
  const llmProfiles = brought(content.llmProfiles, profiles);
  for (const step of steps.flatMap(stepTree)) {
    const slot = step["connection"];
    if (typeof slot === "string" && Object.hasOwn(connections.rename, slot))
      step["connection"] = connections.rename[slot];
    const profile = step["profile"];
    if (
      step.kind === "llm" &&
      typeof profile === "string" &&
      Object.hasOwn(llmProfiles.rename, profile)
    )
      step["profile"] = llmProfiles.rename[profile];
  }
  const ownedAdds = ownActions(workflow, steps, content.actions, target);
  // 4. References the new place can't read.
  const draft = structuredClone(workflow);
  sequencesOf(draft)
    .get(at.owner)!
    .splice(at.index, 0, ...structuredClone(steps));
  const present = new Set(
    [...sequencesOf(draft).values()].flat().map((step) => step.id),
  );
  const unreadable: Unreadable[] = [];
  for (const step of steps.flatMap(stepTree))
    for (const site of refSites(step)) {
      const match = /^\/steps\/([^/]+)/.exec(site.ref);
      if (!match) continue;
      const visible = visibleSteps(draft, step.id, site.fieldPath);
      if (visible?.some((item) => item.id === match[1])) continue;
      unreadable.push({
        stepId: step.id,
        ...site,
        target: match[1],
        reason: present.has(match[1]) ? "later" : "missing",
      });
    }
  // 5. Pins follow their steps' new IDs; pins recorded from runs never come.
  const pastedIds = new Set(steps.flatMap(stepTree).map((step) => step.id));
  const pins = Object.fromEntries(
    Object.entries(content.pins).flatMap(
      ([key, entry]): [string, unknown][] => {
        if (!isRecord(entry) || runDerived(entry)) return [];
        const owner = instanceStep(key);
        const next = renamed.get(owner);
        const rekeyed = next ? `${next}${key.slice(owner.length)}` : key;
        return pastedIds.has(instanceStep(rekeyed)) ? [[rekeyed, entry]] : [];
      },
    ),
  );
  return {
    at,
    steps,
    ids: steps.map((step) => step.id),
    renamed: Object.fromEntries(renamed),
    addConnections: connections.add,
    addProfiles: llmProfiles.add,
    renamedSlots: connections.rename,
    renamedProfiles: llmProfiles.rename,
    pins,
    ownedAdds,
    unreadable,
  };
}

/** The reference is still there and still can't be read where the step sits. */
export function stillUnreadable(
  workflow: Workflow,
  step: Step,
  gap: Unreadable,
): boolean {
  const value = gap.path.reduce<unknown>(
    (current, segment) =>
      isRecord(current) || Array.isArray(current)
        ? (current as Record<string | number, unknown>)[segment]
        : undefined,
    step,
  );
  if (!isRecord(value) || value["ref"] !== gap.ref) return false;
  return !visibleSteps(workflow, step.id, gap.fieldPath)?.some(
    (item) => item.id === gap.target,
  );
}

/** "3 references can't be read here (check isn't in this workflow)." */
export function unreadableMessage(list: readonly Unreadable[]): string {
  const first = list[0];
  const why =
    first.reason === "missing"
      ? `${shown(first.target)} isn't in this workflow`
      : `${shown(first.target)} runs later or on another path`;
  return `${list.length === 1 ? "1 reference" : `${list.length} references`} can't be read here (${why}).`;
}
