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
import { parseDocument, stringify, isMap, isSeq, isNode, isScalar } from "yaml";
import { computeLaneLayout } from "./designer/lane-layout";
export type Kind =
  | "action"
  | "decisionTable"
  | "llm"
  | "transform"
  | "switch"
  | "parallel"
  | "wait"
  | "signal"
  | "fail"
  | "humanTask";
export interface Step {
  id: string;
  kind: Kind;
  [key: string]: unknown;
}
export interface Branch {
  steps: Step[];
  output: unknown;
  when?: unknown;
}
export interface Workflow {
  apiVersion: string;
  kind: string;
  metadata: { name: string; version: string };
  spec: { steps: Step[]; [key: string]: unknown };
  [key: string]: unknown;
}
export interface Point {
  x: number;
  y: number;
}
export interface Node {
  step: Step;
  owner: string;
  index: number;
  depth: number;
  point: Point;
  /** A canvas-only invitation to add answer paths after an unrouted human task. */
  answerPrompt?: string;
}
export interface Target {
  owner: string;
  index: number;
  point: Point;
  label: string;
  /** The branch name shown on the placeholder card of an empty branch. */
  empty?: string;
  /** Visible destination on a lane's final insertion target. */
  lane?: string;
}
/** The dashed card that stands for an empty branch on the canvas. */
export interface Placeholder {
  owner: string;
  parent: string;
  branch: string;
  point: Point;
}
/** Where a reference sits in the document, and which step (or the workflow) holds it. */
export interface ReferenceSite {
  path: (string | number)[];
  ref: string;
  holder: string;
}
export const stepIdPattern = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;
/** The decisions of a human task that does not list its own. */
export const defaultDecisions = ["approve", "reject"];
export interface Layout {
  revision: number;
  positions: Record<string, Point>;
  viewport: { x: number; y: number; zoom: number };
}
export const kinds: Kind[] = [
  "action",
  "decisionTable",
  "llm",
  "transform",
  "switch",
  "parallel",
  "wait",
  "signal",
  "humanTask",
  "fail",
];
/** The readable start of a new step's ID: decision-1, approval-1, call-action-1. */
export const stepIdPrefix: Record<Kind, string> = {
  action: "call-action",
  decisionTable: "evaluate-rules",
  llm: "ask-ai",
  transform: "transform",
  switch: "decision",
  parallel: "parallel",
  wait: "wait",
  signal: "signal",
  humanTask: "approval",
  fail: "fail",
};
export const freshWorkflow = (): Workflow => ({
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name: "untitled-workflow", version: "1.0.0" },
  spec: {
    inputSchema: { type: "object" },
    outputSchema: { type: "object" },
    steps: [],
    output: { literal: {} },
  },
});
export function createStep(kind: Kind, id: string): Step {
  const branch = () => ({ steps: [], output: { literal: {} } });
  const defaults: Record<Kind, Record<string, unknown>> = {
    action: { uses: "", with: { literal: {} } },
    decisionTable: { uses: "", with: { ref: "/input" } },
    llm: {
      uses: "weave-agentic-generate@1.0.0",
      profile: "default",
      connection: "ai",
      prompt: { literal: "" },
      context: { literal: {} },
    },
    transform: { value: { literal: {} } },
    // A new case has no condition yet: the inspector asks for one ("Choose
    // when this path applies.") instead of a silent "always".
    switch: {
      cases: [branch()],
      default: branch(),
    },
    parallel: {
      branches: { first: branch(), second: branch() },
      concurrency: 2,
    },
    wait: { durationSeconds: 60 },
    signal: {
      name: "",
      timeoutSeconds: 3600,
      payloadSchema: { type: "object" },
    },
    fail: { code: "business-error", message: "" },
    humanTask: {
      title: { literal: "Review request" },
      context: { literal: {} },
      assignment: "reviewers",
      formSchema: { type: "object", properties: {} },
      decisions: ["approve", "reject"],
    },
  };
  return { id, kind, ...defaults[kind] };
}
function branches(step: Step): [string, Branch][] {
  if (step.kind === "switch")
    return [
      ...(step["cases"] as Branch[]).map((b, i): [string, Branch] => [
        `case ${i + 1}`,
        b,
      ]),
      ["default", step["default"] as Branch],
    ];
  if (step.kind === "parallel")
    return Object.entries(step["branches"] as Record<string, Branch>);
  return [];
}
function preserveYamlComments(old: unknown, replacement: unknown): void {
  if (!isNode(old) || !isNode(replacement)) return;
  replacement.comment = old.comment;
  replacement.commentBefore = old.commentBefore;
  replacement.spaceBefore = old.spaceBefore;
  if (isMap(old) && isMap(replacement)) {
    for (const pair of replacement.items) {
      const original = old.items.find(
        (p) => String(p.key) === String(pair.key),
      );
      if (original) {
        preserveYamlComments(original.key, pair.key);
        preserveYamlComments(original.value, pair.value);
      }
    }
  } else if (isSeq(old) && isSeq(replacement)) {
    for (let i = 0; i < replacement.items.length; i++) {
      const item = replacement.items[i];
      const id = isMap(item) ? item.get("id") : undefined;
      const original =
        id === undefined
          ? old.items[i]
          : old.items.find((n) => isMap(n) && n.get("id") === id);
      preserveYamlComments(original, item);
    }
  }
}
/** Plain name of a branch: "Case 1", "Otherwise", or a parallel branch's own name. */
export function branchTitle(step: Step, name: string) {
  if (step.kind === "switch")
    return name === "default" ? "Otherwise" : `Case ${name.slice(5)}`;
  return name;
}
/**
 * Where a step sits, in words: "Main sequence", "Case 1 of route",
 * "Otherwise of route" or "first of fulfil".
 */
export function ownerLabel(owner: string, steps?: ReadonlyMap<string, Step>) {
  if (owner === "root") return "Main sequence";
  const split = owner.indexOf("/");
  const parent = owner.slice(0, split);
  const name = owner.slice(split + 1);
  const step = steps?.get(parent);
  const title = step
    ? branchTitle(step, name)
    : name.startsWith("case ")
      ? `Case ${name.slice(5)}`
      : name === "default"
        ? "Otherwise"
        : name;
  return `${title} of ${parent}`;
}
const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
/** Collects `ref` values of an expression; `literal` content is data, never a reference. */
function expressionRefs(
  expression: unknown,
  path: (string | number)[],
  holder: string,
  out: ReferenceSite[],
) {
  if (!isRecord(expression)) return;
  const keys = Object.keys(expression);
  if (keys.length !== 1) return;
  const body = expression[keys[0]];
  if (keys[0] === "ref" && typeof body === "string")
    out.push({ path: [...path, "ref"], ref: body, holder });
  else if (keys[0] === "object" && isRecord(body))
    for (const key of Object.keys(body))
      expressionRefs(body[key], [...path, "object", key], holder, out);
  else if (keys[0] === "array" && Array.isArray(body))
    body.forEach((item, i) =>
      expressionRefs(item, [...path, "array", i], holder, out),
    );
  else if (keys[0] === "op" && isRecord(body) && Array.isArray(body["args"]))
    body["args"].forEach((arg, i) =>
      expressionRefs(arg, [...path, "op", "args", i], holder, out),
    );
}
/** The expression fields of a step, as paths relative to the step. */
function expressionPaths(step: Step): (string | number)[][] {
  switch (step.kind) {
    case "action":
    case "decisionTable":
      return [["with"]];
    case "llm":
      return [["prompt"], ["context"]];
    case "transform":
      return [["value"]];
    case "humanTask":
      return [["title"], ["context"]];
    case "switch":
      return [
        ...(Array.isArray(step["cases"]) ? step["cases"] : []).flatMap(
          (_, i) => [
            ["cases", i, "when"],
            ["cases", i, "output"],
          ],
        ),
        ["default", "output"],
      ];
    case "parallel":
      return Object.keys(
        isRecord(step["branches"]) ? step["branches"] : {},
      ).map((name) => ["branches", name, "output"]);
    default:
      return [];
  }
}
const readPath = (root: unknown, path: (string | number)[]) =>
  path.reduce<unknown>(
    (value, key) =>
      value && typeof value === "object"
        ? (value as Record<string | number, unknown>)[key]
        : undefined,
    root,
  );
/** True when a pointer reads one of these steps (`/steps/<id>` or below it). */
const readsStep = (ref: string, ids: Set<string>) => {
  const match = /^\/steps\/([^/]+)(?:\/|$)/.exec(ref);
  return !!match && ids.has(match[1]);
};
/** "a", "a and b", "a, b and c". */
const listNames = (names: string[]) =>
  names.length < 2
    ? (names[0] ?? "")
    : `${names.slice(0, -1).join(", ")} and ${names.at(-1)}`;
export class StructuredCanvasAdapter {
  definition: Workflow = freshWorkflow();
  source = stringify(this.definition);
  format: "yaml" | "json" = "yaml";
  error = "";
  readonly = false;
  layout: Layout = {
    revision: 0,
    positions: {},
    viewport: { x: 0, y: 0, zoom: 1 },
  };
  selected = "";
  unplaced: Step[] = [];
  private past: string[] = [];
  private future: string[] = [];
  /** Inside batch(): edits share the batch's single undo step. */
  private batching = false;
  /**
   * Counts edits, undos and redos. A delayed "Undo" (a toast's) acts only
   * while nothing else changed the workflow since.
   */
  get revision() {
    return this.edits;
  }
  private edits = 0;
  private checkpoint() {
    if (this.batching) return;
    this.edits++;
    this.past.push(this.snapshot());
    if (this.past.length > 100) this.past.shift();
    this.future = [];
  }
  private snapshot() {
    return JSON.stringify({
      definition: this.definition,
      source: this.source,
      format: this.format,
      error: this.error,
      readonly: this.readonly,
      layout: this.layout,
      selected: this.selected,
      unplaced: this.unplaced,
    });
  }
  private restore(s: string) {
    Object.assign(this, JSON.parse(s));
  }
  /**
   * Runs several edits as one undo step, for example declaring a connection
   * slot and inserting the step that uses it. When an edit fails, the
   * workflow and the undo history are left as they were.
   */
  batch<T>(edits: () => T): T {
    if (this.batching) return edits();
    const before = this.snapshot();
    const past = [...this.past];
    const future = [...this.future];
    this.checkpoint();
    this.batching = true;
    try {
      return edits();
    } catch (error) {
      this.restore(before);
      this.past = past;
      this.future = future;
      throw error;
    } finally {
      this.batching = false;
    }
  }
  /** Merge derived values into an unchanged initiating edit's undo entry. */
  continueEdit<T>(revision: number, edits: () => T): T {
    if (this.batching) return edits();
    if (this.revision !== revision || !this.past.length)
      return this.batch(edits);
    const before = this.snapshot();
    this.batching = true;
    try {
      return edits();
    } catch (error) {
      this.restore(before);
      throw error;
    } finally {
      this.batching = false;
    }
  }
  /**
   * Counts the workflows opened in this editor. Work that started for one
   * (for example an insert waiting for an action's details) checks it before
   * it changes the workflow open now.
   */
  get opened() {
    return this.openings;
  }
  private openings = 0;
  /** Forgets undo and redo; used when another workflow is opened. */
  clearHistory() {
    this.past = [];
    this.future = [];
    this.openings++;
  }
  get canUndo() {
    return this.past.length > 0;
  }
  get canRedo() {
    return this.future.length > 0;
  }
  undo() {
    const s = this.past.pop();
    if (s) {
      this.edits++;
      this.future.push(this.snapshot());
      this.restore(s);
    }
  }
  redo() {
    const s = this.future.pop();
    if (s) {
      this.edits++;
      this.past.push(this.snapshot());
      this.restore(s);
    }
  }
  setSource(source: string, format = this.format) {
    this.checkpoint();
    this.source = source;
    this.format = format;
    this.error = "";
    try {
      const doc = parseDocument(source, {
        uniqueKeys: true,
        maxAliasCount: 0,
      } as never);
      if (doc.errors.length) throw Error(doc.errors[0].message);
      const value = (
        format === "json" ? JSON.parse(source) : doc.toJS({ maxAliasCount: 0 })
      ) as Workflow;
      if (
        !value ||
        value.kind !== "Workflow" ||
        !Array.isArray(value.spec?.steps) ||
        !value.metadata
      )
        throw Error("Expected a Workflow with metadata and spec.steps.");
      this.assertStructure(value);
      this.definition = value;
      this.readonly = false;
    } catch (e) {
      this.error = e instanceof Error ? e.message : "Invalid source";
      this.readonly = true;
    }
  }
  private assertStructure(value: Workflow) {
    const ids = new Set<string>();
    const visit = (steps: Step[], depth: number) => {
      if (depth > 40)
        throw Error(
          "Nesting exceeds the visual editor limit. Keep editing in source.",
        );
      for (const s of steps) {
        if (!s.id || ids.has(s.id)) throw Error("Steps must have unique IDs.");
        ids.add(s.id);
        if (!kinds.includes(s.kind))
          throw Error(
            `Unsupported step ${s.kind}; source is preserved. The last valid graph is read-only.`,
          );
        for (const [, b] of branches(s)) {
          if (!b || !Array.isArray(b.steps))
            throw Error(`Invalid branches in ${s.id}.`);
          visit(b.steps, depth + 1);
        }
      }
    };
    visit(value.spec.steps, 0);
  }
  private sync() {
    this.source =
      this.format === "json"
        ? JSON.stringify(this.definition, null, 2)
        : stringify(this.definition);
    this.error = "";
  }
  get hasComments() {
    return (
      this.format === "yaml" &&
      parseDocument(this.source).toString().includes("#")
    );
  }
  // Structural edits preserve YAML comments by updating only the affected CST value.
  private syncSteps() {
    if (this.format === "yaml") {
      const doc = parseDocument(this.source);
      const previous = doc.getIn(["spec", "steps"]);
      const next = doc.createNode(this.definition.spec.steps);
      preserveYamlComments(previous, next);
      doc.setIn(["spec", "steps"], next);
      this.source = doc.toString();
    } else this.sync();
  }
  owners(): Map<string, Step[]> {
    const map = new Map<string, Step[]>([["root", this.definition.spec.steps]]);
    const visit = (ss: Step[]) =>
      ss.forEach((s) =>
        branches(s).forEach(([name, b]) => {
          map.set(`${s.id}/${name}`, b.steps);
          visit(b.steps);
        }),
      );
    visit(this.definition.spec.steps);
    return map;
  }
  private computeLayout() {
    return computeLaneLayout(this.definition, this.layout.positions);
  }
  canvasLayout() {
    return this.computeLayout();
  }
  laneGroups() {
    return this.computeLayout().groups;
  }
  junctions() {
    return this.computeLayout().junctions;
  }
  terminals() {
    return this.computeLayout().terminals;
  }
  nodes(): Node[] {
    return this.computeLayout().nodes;
  }
  placeholders(): Placeholder[] {
    return this.computeLayout().placeholders;
  }
  /**
   * Semantic edges by default. The designer includes split/join junctions and
   * empty-branch cards, without adding synthetic steps to the document.
   */
  visualConnections(placeholders = false) {
    if (placeholders) return this.computeLayout().edges;
    const nodes = this.nodes();
    const owners = new Map<string, Node[]>();
    for (const node of nodes) {
      const list = owners.get(node.owner) ?? [];
      list.push(node);
      owners.set(node.owner, list);
    }
    const continuation = (node: Node): string => {
      const next = owners.get(node.owner)?.[node.index + 1];
      if (next) return next.step.id;
      const parent = nodes.find((n) => n.step.id === node.owner.split("/")[0]);
      return parent ? continuation(parent) : "$end";
    };
    const links = [
      { from: "$start", to: owners.get("root")?.[0]?.step.id ?? "$end" },
    ];
    for (const node of nodes) {
      if (node.step.kind === "fail") continue;
      const childBranches = branches(node.step);
      if (childBranches.length) {
        for (const [name] of childBranches) {
          const owner = `${node.step.id}/${name}`;
          const first = owners.get(owner)?.[0]?.step.id;
          if (first) links.push({ from: node.step.id, to: first });
          else links.push({ from: node.step.id, to: continuation(node) });
        }
      } else links.push({ from: node.step.id, to: continuation(node) });
    }
    return links;
  }
  boundaries() {
    return this.computeLayout().boundaries;
  }
  targets(): Target[] {
    return this.computeLayout().targets;
  }
  insert(
    kind: Kind,
    owner = "root",
    index?: number,
    fields: Record<string, unknown> = {},
  ) {
    if (this.readonly) throw Error("Fix source before editing the graph.");
    const steps = this.owners().get(owner);
    if (!steps) throw Error("Target branch no longer exists.");
    this.checkpoint();
    const s = {
      ...createStep(kind, this.freeId(stepIdPrefix[kind])),
      ...fields,
    };
    steps.splice(index ?? steps.length, 0, s);
    this.selected = s.id;
    this.syncSteps();
    return s;
  }
  /** The first unused ID of the form `<prefix>-<n>`. */
  private freeId(prefix: string, taken: ReadonlySet<string> = new Set()) {
    const used = new Set([
      ...this.nodes().map((n) => n.step.id),
      ...this.unplaced.map((s) => s.id),
      ...taken,
    ]);
    let i = 1;
    while (used.has(`${prefix}-${i}`)) i++;
    return `${prefix}-${i}`;
  }
  /**
   * Inserts a copy of a step right after it, as one undo step. The copy and
   * every step inside it get new IDs, and references inside the copy to the
   * copied steps follow them; references to other steps stay as they are.
   * Returns the copy's ID.
   */
  duplicate(id: string) {
    if (this.readonly) throw Error("Fix source before editing the graph.");
    const node = this.nodes().find((n) => n.step.id === id);
    if (!node) throw Error("Select a placed step.");
    const renamed = new Map<string, string>();
    const taken = new Set<string>();
    for (const old of this.subtree(node.step)) {
      const base = old.replace(/-\d+$/, "") || old;
      const next = this.freeId(base, taken);
      taken.add(next);
      renamed.set(old, next);
    }
    const rewrite = (value: unknown, key = ""): unknown => {
      if (Array.isArray(value)) return value.map((item) => rewrite(item));
      if (isRecord(value))
        return Object.fromEntries(
          Object.entries(value).map(([k, v]) => [k, rewrite(v, k)]),
        );
      if (key === "ref" && typeof value === "string") {
        const match = /^\/steps\/([^/]+)(.*)$/.exec(value);
        if (match && renamed.has(match[1]))
          return `/steps/${renamed.get(match[1])}${match[2]}`;
      }
      return value;
    };
    const copy = rewrite(structuredClone(node.step)) as Step;
    const renameSteps = (step: Step) => {
      step.id = renamed.get(step.id) ?? step.id;
      branches(step).forEach(([, b]) => b.steps.forEach(renameSteps));
    };
    renameSteps(copy);
    this.checkpoint();
    this.owners()
      .get(node.owner)!
      .splice(node.index + 1, 0, copy);
    this.selected = copy.id;
    this.syncSteps();
    return copy.id;
  }
  addUnplaced(kind: Kind) {
    this.checkpoint();
    const s = createStep(kind, `unplaced-${crypto.randomUUID().slice(0, 8)}`);
    this.unplaced.push(s);
    this.selected = s.id;
  }
  place(id: string, owner: string, index: number) {
    const n = this.unplaced.find((s) => s.id === id);
    const steps = this.owners().get(owner);
    if (!n || !steps) return;
    this.checkpoint();
    steps.splice(index, 0, n);
    this.unplaced = this.unplaced.filter((s) => s !== n);
    this.syncSteps();
  }
  move(id: string, owner: string, index: number) {
    const node = this.nodes().find((n) => n.step.id === id),
      dest = this.owners().get(owner);
    if (!node || !dest) throw Error("Select an existing step and destination.");
    if (owner.startsWith(`${id}/`))
      throw Error("A step cannot contain itself.");
    const childIds = new Set<string>();
    const gather = (s: Step) => {
      childIds.add(s.id);
      branches(s).forEach(([, b]) => b.steps.forEach(gather));
    };
    gather(node.step);
    if ([...childIds].some((x) => owner.startsWith(`${x}/`)))
      throw Error("Cannot move a group into its own branch.");
    this.checkpoint();
    const from = this.owners().get(node.owner)!;
    from.splice(node.index, 1);
    dest.splice(
      owner === node.owner && index > node.index ? index - 1 : index,
      0,
      node.step,
    );
    this.syncSteps();
  }
  position(id: string, point: Point, snap = true) {
    this.checkpoint();
    this.layout.positions = {
      ...this.layout.positions,
      [id]: snap
        ? { x: Math.round(point.x / 16) * 16, y: Math.round(point.y / 16) * 16 }
        : point,
    };
    this.layout.revision++;
  }
  /** Every reference in the workflow's expressions, with the step that holds it. */
  referenceSites(): ReferenceSite[] {
    const out: ReferenceSite[] = [];
    const visit = (steps: Step[], path: (string | number)[]) =>
      steps.forEach((step, i) => {
        const at = [...path, i];
        for (const field of expressionPaths(step))
          expressionRefs(
            readPath(step, field),
            [...at, ...field],
            step.id,
            out,
          );
        if (step.kind === "switch") {
          (Array.isArray(step["cases"])
            ? (step["cases"] as Branch[])
            : []
          ).forEach((b, j) =>
            visit(b.steps ?? [], [...at, "cases", j, "steps"]),
          );
          const fallback = step["default"] as Branch | undefined;
          visit(fallback?.steps ?? [], [...at, "default", "steps"]);
        } else if (step.kind === "parallel")
          for (const [name, b] of Object.entries(
            (step["branches"] ?? {}) as Record<string, Branch>,
          ))
            visit(b.steps ?? [], [...at, "branches", name, "steps"]);
      });
    visit(this.definition.spec.steps, ["spec", "steps"]);
    expressionRefs(this.definition.spec["output"], ["spec", "output"], "", out);
    return out;
  }
  /** Ids of a step and every step nested in its branches. */
  private subtree(step: Step) {
    const ids = new Set<string>();
    const gather = (s: Step) => {
      ids.add(s.id);
      branches(s).forEach(([, b]) => b.steps.forEach(gather));
    };
    gather(step);
    return ids;
  }
  /** Number of steps nested inside a group's branches. */
  containedSteps(id: string) {
    const step = this.nodes().find((n) => n.step.id === id)?.step;
    return step ? this.subtree(step).size - 1 : 0;
  }
  /**
   * Deletes a step. A group is deleted only when its branches are empty,
   * unless `contents` is set. Steps (or the workflow output) that still read
   * a deleted step are named and block the deletion.
   */
  remove(id: string, options: { contents?: boolean } = {}) {
    const n = this.nodes().find((n) => n.step.id === id);
    if (!n) {
      this.checkpoint();
      this.unplaced = this.unplaced.filter((s) => s.id !== id);
      return;
    }
    if (!options.contents && branches(n.step).some(([, b]) => b.steps.length))
      throw Error(
        "Move or delete the contained branch steps before deleting this group.",
      );
    const removed = this.subtree(n.step);
    const holders = [
      ...new Set(
        this.referenceSites()
          .filter((site) => !removed.has(site.holder))
          .filter((site) => readsStep(site.ref, removed))
          .map((site) => site.holder),
      ),
    ];
    if (holders.length) {
      const names = holders
        .filter(Boolean)
        .concat(holders.includes("") ? ["the workflow output"] : []);
      const group = removed.size > 1;
      throw Error(
        `${group ? "Steps in this group are" : "This step is"} referenced by ${listNames(names)}. Update ${names.length === 1 ? "it" : "them"} before deleting ${group ? "the group" : "it"}.`,
      );
    }
    this.checkpoint();
    this.owners().get(n.owner)!.splice(n.index, 1);
    const positions = { ...this.layout.positions };
    for (const removedId of removed) delete positions[removedId];
    this.layout.positions = positions;
    if (removed.has(this.selected)) this.selected = "";
    this.syncSteps();
  }
  /**
   * Renames a step and rewrites every reference to it (`/steps/<old>` and
   * below) in expressions; literal text is left alone. YAML comments are kept
   * because only the changed scalars are replaced. Returns the number of
   * references rewritten.
   */
  renameStep(oldId: string, newId: string) {
    if (this.readonly) throw Error("Fix source before editing the graph.");
    const next = newId.trim();
    if (next === oldId) return 0;
    if (!stepIdPattern.test(next))
      throw Error(
        "Use letters, numbers, dots, underscores or hyphens for the step ID, starting with a letter or number.",
      );
    const ids = new Set([
      ...this.nodes().map((n) => n.step.id),
      ...this.unplaced.map((s) => s.id),
    ]);
    if (!ids.has(oldId)) throw Error("Select an existing step.");
    if (ids.has(next)) throw Error(`Another step is already named ${next}.`);
    const prefix = `/steps/${oldId}`;
    const sites = this.referenceSites().filter(
      (site) => site.ref === prefix || site.ref.startsWith(`${prefix}/`),
    );
    let stepPath: (string | number)[] | null = null;
    const find = (steps: Step[], path: (string | number)[]) =>
      steps.forEach((step, i) => {
        if (stepPath) return;
        if (step.id === oldId) {
          stepPath = [...path, i];
          return;
        }
        if (step.kind === "switch") {
          (step["cases"] as Branch[]).forEach((b, j) =>
            find(b.steps, [...path, i, "cases", j, "steps"]),
          );
          find((step["default"] as Branch).steps, [
            ...path,
            i,
            "default",
            "steps",
          ]);
        } else if (step.kind === "parallel")
          for (const [name, b] of Object.entries(
            step["branches"] as Record<string, Branch>,
          ))
            find(b.steps, [...path, i, "branches", name, "steps"]);
      });
    find(this.definition.spec.steps, ["spec", "steps"]);
    this.checkpoint();
    this.definition = structuredClone(this.definition);
    const changes: [(string | number)[], string][] = sites.map((site) => [
      site.path,
      `/steps/${next}${site.ref.slice(prefix.length)}`,
    ]);
    if (stepPath)
      changes.push([[...(stepPath as (string | number)[]), "id"], next]);
    else
      this.unplaced = this.unplaced.map((s) =>
        s.id === oldId ? { ...s, id: next } : s,
      );
    const doc = this.format === "yaml" ? parseDocument(this.source) : null;
    for (const [path, value] of changes) {
      const parent = readPath(this.definition, path.slice(0, -1)) as Record<
        string | number,
        unknown
      >;
      parent[path.at(-1)!] = value;
      if (!doc) continue;
      // Replacing only the scalar's value keeps its inline comment.
      const node = doc.getIn(path, true);
      if (isScalar(node)) node.value = value;
      else doc.setIn(path, value);
    }
    if (doc) {
      this.source = doc.toString();
      this.error = "";
    } else this.sync();
    if (oldId in this.layout.positions) {
      const positions = { ...this.layout.positions };
      positions[next] = positions[oldId];
      delete positions[oldId];
      this.layout.positions = positions;
      this.layout.revision++;
    }
    if (this.selected === oldId) this.selected = next;
    return sites.length;
  }
  renameDecision(taskId: string, oldAnswer: string, newAnswer: string) {
    if (this.readonly) throw Error("Fix source before editing the graph.");
    const task = this.nodes().find((node) => node.step.id === taskId)?.step;
    if (!task || task.kind !== "humanTask") throw Error("Select a human task.");
    const answers =
      task["decisions"] === undefined
        ? [...defaultDecisions]
        : task["decisions"];
    const next = newAnswer.trim();
    if (
      !Array.isArray(answers) ||
      answers.length < 1 ||
      answers.length > 32 ||
      !answers.includes(oldAnswer) ||
      !stepIdPattern.test(next) ||
      (next !== oldAnswer && answers.includes(next))
    )
      throw Error(
        "Use a unique answer name with letters, numbers, dots, underscores or hyphens.",
      );
    if (next === oldAnswer) return 0;
    let changed = 0;
    const rewrite = (value: unknown): void => {
      if (!value || typeof value !== "object" || Array.isArray(value)) return;
      const expression = value as Record<string, unknown>;
      if ("literal" in expression || "ref" in expression) return;
      const operation = expression["op"] as
        | { name?: string; args?: unknown[] }
        | undefined;
      if (operation && Array.isArray(operation.args)) {
        if (operation.name === "eq" && operation.args.length === 2) {
          for (const index of [0, 1]) {
            const reference = operation.args[index] as Record<
              string,
              unknown
            > | null;
            const literal = operation.args[1 - index] as Record<
              string,
              unknown
            > | null;
            if (
              reference?.["ref"] === `/steps/${taskId}/output/decision` &&
              literal?.["literal"] === oldAnswer
            ) {
              literal["literal"] = next;
              changed++;
            }
          }
        }
        operation.args.forEach(rewrite);
      }
    };
    this.batch(() => {
      this.update(
        taskId,
        JSON.stringify({
          ...task,
          decisions: answers.map((answer) =>
            answer === oldAnswer ? next : answer,
          ),
        }),
      );
      // Resolve each switch again after updates so nested replacements cannot restore an old ancestor.
      const switches = this.nodes()
        .filter((node) => node.step.kind === "switch")
        .map((node) => node.step.id);
      for (const id of switches) {
        const step = structuredClone(
          this.nodes().find((node) => node.step.id === id)!.step,
        );
        const before = changed;
        for (const branch of step["cases"] as Branch[]) rewrite(branch["when"]);
        if (changed !== before) this.update(id, JSON.stringify(step));
      }
    });
    return changed;
  }
  /**
   * Adds a decision after a human task with one case per decision, each
   * testing `/steps/<task>/output/decision`.
   */
  branchOnDecision(id: string) {
    if (this.readonly) throw Error("Fix source before editing the graph.");
    const node = this.nodes().find((n) => n.step.id === id);
    if (!node || node.step.kind !== "humanTask")
      throw Error("Select a human task.");
    // A task that lists no decisions offers the language's defaults.
    const decisions = Array.isArray(node.step["decisions"])
      ? (node.step["decisions"] as unknown[]).map(String)
      : node.step["decisions"] === undefined
        ? defaultDecisions
        : [];
    if (!decisions.length)
      throw Error("Add the decisions people can make, then branch on them.");
    const step = this.insert("switch", node.owner, node.index + 1);
    step["cases"] = decisions.map((decision) => ({
      when: {
        op: {
          name: "eq",
          args: [
            { ref: `/steps/${id}/output/decision` },
            { literal: decision },
          ],
        },
      },
      steps: [],
      output: { literal: {} },
    }));
    this.syncSteps();
    return step;
  }
  update(id: string, text: string) {
    const n = this.nodes().find((n) => n.step.id === id);
    if (!n) throw Error("Select a placed step.");
    const replacement = JSON.parse(text) as Step;
    if (replacement.id !== id)
      throw Error("Rename in source with all dependent references.");
    const d = structuredClone(this.definition);
    const original = this.definition;
    this.definition = d;
    this.owners().get(n.owner)![n.index] = replacement;
    try {
      this.assertStructure(d);
    } catch (e) {
      this.definition = original;
      throw e;
    }
    this.definition = original;
    this.checkpoint();
    this.definition = d;
    this.syncSteps();
  }
  autoLayout() {
    if (!Object.keys(this.layout.positions).length) return;
    this.checkpoint();
    this.layout.positions = {};
    this.layout.revision++;
  }
  editBranches(
    id: string,
    operation: "add" | "remove" | "rename" | "up" | "down",
    name?: string,
    replacement?: string,
  ) {
    if (this.readonly) throw Error("Fix source before editing the graph.");
    const step = this.nodes().find((n) => n.step.id === id)?.step;
    if (!step || !["switch", "parallel"].includes(step.kind))
      throw Error("Select a decision or parallel group.");
    const draft = structuredClone(step);
    const empty = () => ({ steps: [], output: { literal: {} } });
    if (draft.kind === "parallel") {
      const groups = draft["branches"] as Record<string, Branch>;
      if (operation === "add") {
        let n = 1;
        while (`branch-${n}` in groups) n++;
        groups[`branch-${n}`] = empty();
      } else {
        if (!name || !groups[name]) throw Error("Branch no longer exists.");
        if (
          operation === "remove" &&
          (groups[name].steps.length || Object.keys(groups).length === 1)
        )
          throw Error(
            "Move contained steps first and keep at least one branch.",
          );
        if (
          ["remove", "rename"].includes(operation) &&
          JSON.stringify(this.definition).includes(
            `/steps/${id}/output/${name.replace(/~/g, "~0").replace(/\//g, "~1")}`,
          )
        )
          throw Error("Update references to this branch in source first.");
        if (operation === "rename") {
          if (
            !replacement ||
            !/^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(replacement) ||
            replacement in groups
          )
            throw Error(
              "Use a unique branch name with letters, numbers, dots, underscores or hyphens.",
            );
          draft["branches"] = Object.fromEntries(
            Object.entries(groups).map(([key, value]) => [
              key === name ? replacement : key,
              value,
            ]),
          );
        } else if (operation === "remove") delete groups[name];
      }
    } else {
      const cases = draft["cases"] as Branch[];
      // A new case starts without a condition; the inspector asks for one.
      if (operation === "add") cases.push(empty());
      else {
        const index = Number(name);
        if (!Number.isInteger(index) || !cases[index])
          throw Error("Case no longer exists.");
        if (operation === "remove") {
          if (cases[index].steps.length || cases.length === 1)
            throw Error(
              "Move contained steps first and keep at least one case.",
            );
          cases.splice(index, 1);
        } else {
          const next = index + (operation === "up" ? -1 : 1);
          if (next < 0 || next >= cases.length) return;
          [cases[index], cases[next]] = [cases[next], cases[index]];
        }
      }
    }
    this.update(id, JSON.stringify(draft));
  }
  updateWorkflow(document: Workflow) {
    if (this.readonly) throw Error("Fix source before editing the graph.");
    const definition = structuredClone(document);
    definition.spec.steps = this.definition.spec.steps;
    this.assertStructure(definition);
    const doc = this.format === "yaml" ? parseDocument(this.source) : null;
    if (doc) {
      const set = (path: string[], value: unknown) => {
        if (value === undefined) {
          doc.deleteIn(path);
          return;
        }
        const next = doc.createNode(value);
        preserveYamlComments(doc.getIn(path, true), next);
        doc.setIn(path, next);
      };
      set(["metadata"], definition.metadata);
      const keys = new Set([
        ...Object.keys(this.definition.spec),
        ...Object.keys(definition.spec),
      ]);
      for (const key of keys)
        if (key !== "steps") set(["spec", key], definition.spec[key]);
    }
    this.checkpoint();
    this.definition = definition;
    this.source = doc ? doc.toString() : JSON.stringify(definition, null, 2);
  }
  renameWorkflow(name: string) {
    if (this.readonly) throw Error("Fix source before editing the graph.");
    const doc = this.format === "yaml" ? parseDocument(this.source) : null;
    doc?.setIn(["metadata", "name"], name);
    this.checkpoint();
    this.definition.metadata.name = name;
    this.source = doc
      ? doc.toString()
      : JSON.stringify(this.definition, null, 2);
  }
  /** Opens another workflow; undo never crosses into the previous one. */
  replace(definition: Workflow) {
    this.clearHistory();
    this.definition = structuredClone(definition);
    this.source = stringify(definition);
    this.format = "yaml";
    this.error = "";
    this.readonly = false;
    this.selected = "";
    this.layout = {
      revision: 0,
      positions: {},
      viewport: { x: 0, y: 0, zoom: 1 },
    };
    this.unplaced = [];
  }
}
