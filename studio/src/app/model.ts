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
import { parseDocument, stringify, isMap, isSeq, isNode } from "yaml";
export type Kind =
  | "action"
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
}
export interface Target {
  owner: string;
  index: number;
  point: Point;
  label: string;
}
export interface Layout {
  revision: number;
  positions: Record<string, Point>;
  viewport: { x: number; y: number; zoom: number };
}
export const kinds: Kind[] = [
  "action",
  "transform",
  "switch",
  "parallel",
  "wait",
  "signal",
  "humanTask",
  "fail",
];
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
    action: { uses: "your-action@1.0.0", with: { literal: {} } },
    transform: { value: { literal: {} } },
    switch: {
      cases: [{ when: { literal: true }, ...branch() }],
      default: branch(),
    },
    parallel: {
      branches: { first: branch(), second: branch() },
      concurrency: 2,
    },
    wait: { durationSeconds: 60 },
    signal: {
      name: "message-received",
      timeoutSeconds: 3600,
      payloadSchema: { type: "object" },
    },
    fail: { code: "business-error", message: "Provide a failure reason" },
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
  private checkpoint() {
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
  get canUndo() {
    return this.past.length > 0;
  }
  get canRedo() {
    return this.future.length > 0;
  }
  undo() {
    const s = this.past.pop();
    if (s) {
      this.future.push(this.snapshot());
      this.restore(s);
    }
  }
  redo() {
    const s = this.future.pop();
    if (s) {
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
  nodes(): Node[] {
    const out: Node[] = [];
    let row = 0;
    const visit = (steps: Step[], owner: string, depth: number) => {
      for (let index = 0; index < steps.length; index++) {
        const step = steps[index];
        const point = this.layout.positions[step.id] ?? {
          x: 96 + depth * 272,
          y: 128 + row++ * 120,
        };
        out.push({ step, owner, index, depth, point });
        for (const [name, b] of branches(step))
          visit(b.steps, `${step.id}/${name}`, depth + 1);
      }
    };
    visit(this.definition.spec.steps, "root", 0);
    return out;
  }
  visualConnections() {
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
        for (const [name] of childBranches)
          links.push({
            from: node.step.id,
            to:
              owners.get(`${node.step.id}/${name}`)?.[0]?.step.id ??
              continuation(node),
          });
      } else links.push({ from: node.step.id, to: continuation(node) });
    }
    return links;
  }
  boundaries() {
    const nodes = this.nodes();
    const first = nodes.find((n) => n.owner === "root");
    const x = first?.point.x ?? 96;
    return {
      start: { x, y: (first?.point.y ?? 128) - 104 },
      end: { x, y: Math.max(128, ...nodes.map((n) => n.point.y)) + 128 },
    };
  }
  targets(): Target[] {
    const ns = this.nodes();
    return [...this.owners()].flatMap(([owner, steps]) => {
      const own = ns.filter((n) => n.owner === owner);
      const anchor =
        own[0]?.point ??
        (owner === "root" ? { x: 96, y: 128 } : { x: 368, y: 128 });
      return Array.from({ length: steps.length + 1 }, (_, index) => ({
        owner,
        index,
        point:
          index === 0
            ? { x: anchor.x + 104, y: anchor.y - 34 }
            : {
                x: (own[index - 1]?.point.x ?? anchor.x) + 104,
                y: (own[index - 1]?.point.y ?? anchor.y) + 86,
              },
        label: `Insert ${owner === "root" ? "in sequence" : `in ${owner}`} ${index === 0 ? "at start" : `after ${steps[index - 1].id}`}`,
      }));
    });
  }
  insert(kind: Kind, owner = "root", index?: number) {
    if (this.readonly) throw Error("Fix source before editing the graph.");
    const steps = this.owners().get(owner);
    if (!steps) throw Error("Target branch no longer exists.");
    this.checkpoint();
    const used = new Set(this.nodes().map((n) => n.step.id));
    let i = 1;
    while (used.has(`${kind}-${i}`)) i++;
    const s = createStep(kind, `${kind}-${i}`);
    steps.splice(index ?? steps.length, 0, s);
    this.selected = s.id;
    this.syncSteps();
    return s;
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
  remove(id: string) {
    const n = this.nodes().find((n) => n.step.id === id);
    if (!n) {
      this.checkpoint();
      this.unplaced = this.unplaced.filter((s) => s.id !== id);
      return;
    }
    if (branches(n.step).some(([, b]) => b.steps.length))
      throw Error(
        "Move or delete the contained branch steps before deleting this group.",
      );
    const others = JSON.stringify(this.definition).replace(
      JSON.stringify(n.step),
      "",
    );
    if (others.includes(`/steps/${id}/`))
      throw Error(
        "Other expressions reference this step. Update them before deleting.",
      );
    this.checkpoint();
    this.owners().get(n.owner)!.splice(n.index, 1);
    this.selected = "";
    this.syncSteps();
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
      const cases = draft["cases"] as (Branch & { when: unknown })[];
      if (operation === "add")
        cases.push({ ...empty(), when: { literal: true } });
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
  replace(definition: Workflow) {
    this.checkpoint();
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
