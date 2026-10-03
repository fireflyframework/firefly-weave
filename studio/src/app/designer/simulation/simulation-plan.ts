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
// DOM-free simulation helpers: what the panel reads from a compiled artifact,
// the request bodies it emits, and the values its editors hold. Kept apart
// from the component so the logic is testable against the Python simulator.
import { groupedObject, missingRequired, type Schema } from "../../task-form";
import { durationText, type SimulationEvent } from "./simulation-copy";

export type Json = Record<string, unknown>;
const isRecord = (value: unknown): value is Json =>
  !!value && typeof value === "object" && !Array.isArray(value);

export interface SimulationStep {
  id: string;
  kind: string;
  path: string;
}
/** An action node whose result is mocked under `node:<id>`. */
export interface ActionSlot {
  nodeId: string;
  key: string;
  reference: string;
  outputSchema: Json;
}
/** A signal step; commands address it by its signal name, not its node ID. */
export interface SignalSlot {
  nodeId: string;
  name: string;
  payloadSchema: Json;
  timeoutSeconds: number;
}
/** A human task; decisions address it by its node ID. */
export interface HumanSlot {
  nodeId: string;
  decisions: string[];
  formSchema: Json;
}
export interface SimulationPlan {
  inputSchema: Json;
  steps: SimulationStep[];
  actions: ActionSlot[];
  signals: SignalSlot[];
  humans: HumanSlot[];
}
export interface DebugViewData {
  status: string;
  selected_node?: string | null;
  current_nodes: string[];
  active_nodes: string[];
  variables: Json;
  diagnostics: { code: string; message?: string; path?: string }[];
  events: SimulationEvent[];
  now: string;
}
export interface DebugSessionData {
  id: string;
  revision: number;
  view: DebugViewData;
}
/** Body for POST {project}/debug/sessions. */
export interface DebugCreateBody {
  artifact: Json;
  input: unknown;
  mocks: Record<string, unknown>;
  now: string;
}
/** Body for POST {project}/debug/sessions/{id}/commands. */
export type DebugCommandBody =
  | { kind: "next" }
  | { kind: "continue" }
  | { kind: "advance_time"; seconds: number }
  | { kind: "signal"; name: string; payload: unknown }
  | {
      kind: "human_decision";
      name: string;
      payload: { decision: string; data: unknown };
    }
  | { kind: "breakpoints"; node_ids: string[] };
/** Nodes the canvas should highlight. */
export interface SimulationNodes {
  status: string;
  current: string[];
  active: string[];
  breakpoints: string[];
  /** Steps the simulated run finished, in document order. */
  done: string[];
}
export interface AdvanceOption {
  nodeId: string;
  label: string;
  seconds: number;
}
export interface StepResult {
  stepId: string;
  output: unknown;
}

/** Orders JSON pointers by document position: numbers numerically. */
function comparePaths(a: string, b: string) {
  const left = a.split("/"),
    right = b.split("/");
  for (let i = 0; i < Math.min(left.length, right.length); i++) {
    if (left[i] === right[i]) continue;
    const x = Number(left[i]),
      y = Number(right[i]);
    if (Number.isInteger(x) && Number.isInteger(y)) return x - y;
    return left[i] < right[i] ? -1 : 1;
  }
  return left.length - right.length;
}

/** Reads what the setup and controls need from a compiled artifact. */
export function simulationPlan(artifact: unknown): SimulationPlan {
  const executable = isRecord(artifact) ? artifact["executable"] : undefined;
  const plan: SimulationPlan = {
    inputSchema: {},
    steps: [],
    actions: [],
    signals: [],
    humans: [],
  };
  if (!isRecord(executable)) return plan;
  const schemas = isRecord(executable["schemas"]) ? executable["schemas"] : {};
  const schema = (ref: unknown): Json =>
    isRecord(ref)
      ? ref
      : typeof ref === "string" && isRecord(schemas[ref])
        ? (schemas[ref] as Json)
        : {};
  plan.inputSchema = schema(executable["inputSchema"]);
  const dependencies = Array.isArray(executable["dependencies"])
    ? executable["dependencies"].filter(isRecord)
    : [];
  const graph = isRecord(executable["graph"]) ? executable["graph"] : {};
  const nodes = (Array.isArray(graph["nodes"]) ? graph["nodes"] : [])
    .filter(isRecord)
    .filter((n) => typeof n["id"] === "string" && !n["id"].startsWith("@"))
    .sort((a, b) => comparePaths(String(a["path"]), String(b["path"])));
  for (const node of nodes) {
    const id = String(node["id"]),
      kind = String(node["kind"]);
    plan.steps.push({ id, kind, path: String(node["path"] ?? "") });
    if (kind === "action") {
      const dependency = dependencies.find(
        (d) => d["kind"] === "Action" && d["digest"] === node["dependency"],
      );
      const document = isRecord(dependency?.["document"])
        ? dependency["document"]
        : {};
      const spec = isRecord(document["spec"]) ? document["spec"] : {};
      plan.actions.push({
        nodeId: id,
        key: `node:${id}`,
        reference: String(dependency?.["reference"] ?? ""),
        outputSchema: isRecord(spec["outputSchema"])
          ? spec["outputSchema"]
          : {},
      });
    } else if (kind === "signal")
      plan.signals.push({
        nodeId: id,
        name: String(node["name"]),
        payloadSchema: schema(node["schemaRef"]),
        timeoutSeconds: Number(node["timeoutSeconds"] ?? 0),
      });
    else if (kind === "humanTask")
      plan.humans.push({
        nodeId: id,
        decisions: Array.isArray(node["decisions"])
          ? node["decisions"].map(String)
          : [],
        formSchema: isRecord(node["formSchema"]) ? node["formSchema"] : {},
      });
  }
  return plan;
}

/**
 * A starting value shaped by a schema: constants and first enum options,
 * false, the minimum or zero, empty text and lists, and every required
 * property of an object. Text stays empty so required text must be entered.
 */
export function sampleValue(schema: unknown, depth = 0): unknown {
  if (!isRecord(schema) || depth > 8) return undefined;
  if ("const" in schema) return schema["const"];
  if (Array.isArray(schema["enum"]) && schema["enum"].length)
    return schema["enum"][0];
  const types = Array.isArray(schema["type"])
    ? schema["type"].map(String)
    : [String(schema["type"] ?? "")];
  if (types.includes("object") || isRecord(schema["properties"])) {
    const value: Json = {};
    const properties = isRecord(schema["properties"])
      ? schema["properties"]
      : {};
    const required = Array.isArray(schema["required"])
      ? schema["required"].map(String)
      : [];
    for (const name of required) {
      const field = sampleValue(properties[name], depth + 1);
      if (field !== undefined) value[name] = field;
    }
    return value;
  }
  if (types.includes("boolean")) return false;
  if (types.includes("integer") || types.includes("number")) {
    const minimum = Number(schema["minimum"]);
    if (Number.isFinite(minimum)) return minimum;
    const exclusive = Number(schema["exclusiveMinimum"]);
    if (Number.isFinite(exclusive))
      return types.includes("integer")
        ? Math.floor(exclusive) + 1
        : exclusive + 1;
    return 0;
  }
  if (types.includes("string")) return "";
  if (types.includes("array")) return [];
  if (types.includes("null")) return null;
  return undefined;
}

/** The create body: results are keyed `node:<id>`; skipped ones are left out. */
export function createBody(
  artifact: Json,
  input: unknown,
  results: Record<string, unknown>,
  now: Date,
): DebugCreateBody {
  const mocks: Record<string, unknown> = {};
  for (const [nodeId, value] of Object.entries(results))
    if (value !== undefined) mocks[`node:${nodeId}`] = value;
  return { artifact, input, mocks, now: now.toISOString() };
}

/** "Advance to the deadline" choices for active steps with a deadline. */
export function advanceOptions(
  view: DebugViewData | null | undefined,
  plan: SimulationPlan,
): AdvanceOption[] {
  if (!view) return [];
  const waits = isRecord(view.variables?.["waits"])
    ? (view.variables["waits"] as Json)
    : {};
  const now = Date.parse(view.now);
  const options: AdvanceOption[] = [];
  for (const nodeId of view.active_nodes ?? []) {
    const deadline = Date.parse(String(waits[nodeId] ?? ""));
    if (!Number.isFinite(deadline) || !Number.isFinite(now)) continue;
    const seconds = Math.ceil((deadline - now) / 1000);
    // A deadline already reached needs Continue, not more time: a "0 s"
    // preset would look like a control that does nothing.
    if (seconds <= 0) continue;
    const kind = plan.steps.find((s) => s.id === nodeId)?.kind;
    const span = durationText(seconds);
    const label =
      kind === "wait"
        ? `Finish the wait at ${nodeId} (${span})`
        : kind === "signal"
          ? `Let ${nodeId} time out (${span})`
          : kind === "humanTask"
            ? `Let ${nodeId} expire (${span})`
            : `Reach the deadline of ${nodeId} (${span})`;
    options.push({ nodeId, label, seconds });
  }
  return options.sort((a, b) => a.seconds - b.seconds);
}

/** Step outputs so far, including steps inside branches, in document order. */
export function stepResults(
  view: DebugViewData | null | undefined,
  plan: SimulationPlan,
): StepResult[] {
  if (!view || !isRecord(view.variables)) return [];
  const found = new Map<string, unknown>();
  const collect = (steps: unknown) => {
    if (!isRecord(steps)) return;
    for (const [id, state] of Object.entries(steps))
      if (isRecord(state) && "output" in state && !found.has(id))
        found.set(id, state["output"]);
  };
  collect(view.variables["steps"]);
  const branches = view.variables["branches"];
  if (isRecord(branches))
    for (const branch of Object.values(branches))
      if (isRecord(branch)) collect(branch["steps"]);
  const order = new Map(plan.steps.map((s, i) => [s.id, i]));
  return [...found.entries()]
    .sort(
      ([a], [b]) =>
        (order.get(a) ?? Number.MAX_SAFE_INTEGER) -
        (order.get(b) ?? Number.MAX_SAFE_INTEGER),
    )
    .map(([stepId, output]) => ({ stepId, output }));
}

/** A signal is addressed by its signal name (SignalStep.name), never the node ID. */
export function signalCommand(
  slot: SignalSlot,
  payload: unknown,
): DebugCommandBody {
  return { kind: "signal", name: slot.name, payload };
}
/** A human decision is addressed by the human task's node ID. */
export function decisionCommand(
  human: HumanSlot,
  decision: string,
  data: unknown,
): DebugCommandBody {
  return {
    kind: "human_decision",
    name: human.nodeId,
    payload: { decision, data: data ?? {} },
  };
}
/** Breakpoints are the complete set, in document order. */
export function breakpointsCommand(
  plan: SimulationPlan,
  nodeIds: Iterable<string>,
): DebugCommandBody {
  const wanted = new Set(nodeIds);
  return {
    kind: "breakpoints",
    node_ids: plan.steps.map((s) => s.id).filter((id) => wanted.has(id)),
  };
}

/** Seconds from an amount and a unit; null unless a whole, non-negative count. */
export function advanceSeconds(amount: string, unit: number): number | null {
  if (!/^\d+$/.test(amount.trim())) return null;
  const seconds = Number(amount.trim()) * unit;
  return Number.isSafeInteger(seconds) ? seconds : null;
}

/** A schema-driven value with an optional JSON editor. */
export class ValueEditor {
  /** Object schemas with properties use the form; anything else edits JSON. */
  readonly formCapable: boolean;
  initial: Json;
  value: unknown;
  jsonMode: boolean;
  json: string;
  jsonError = "";
  formValid = true;
  skipped = false;
  constructor(
    readonly schema: Json,
    start: unknown,
  ) {
    this.formCapable = groupedObject(schema as Schema);
    // What the editor shows is what is sent: a schema with no sample value
    // (any JSON) starts as the {} the JSON editor displays, not as undefined,
    // which createBody would drop as if the action had been skipped.
    const value = start === undefined ? {} : start;
    this.value = value;
    this.initial = isRecord(value) ? structuredClone(value) : {};
    this.jsonMode = !this.formCapable;
    this.json = JSON.stringify(value, null, 2);
  }
  missing(): string[] {
    if (this.jsonMode || !this.formCapable) return [];
    return missingRequired(this.schema as Schema, this.value);
  }
  problem(): string {
    if (this.jsonMode) return this.jsonError;
    if (!this.formValid) return "Correct the fields marked with an error.";
    const missing = this.missing();
    return missing.length ? `Fill in ${missing.join(", ")}.` : "";
  }
  formChange(data: Json) {
    this.value = data;
  }
  /**
   * Makes the current value the form's starting data. Call it before a form
   * is shown again (TaskForm restarts from its initial data when created).
   */
  rebase() {
    if (!this.jsonMode && isRecord(this.value))
      this.initial = structuredClone(this.value);
  }
  editJson(text: string) {
    this.json = text;
    try {
      this.value = JSON.parse(text);
      this.jsonError = "";
    } catch {
      this.jsonError = "Enter valid JSON.";
    }
  }
  toggle() {
    if (!this.formCapable) return;
    if (this.jsonMode) {
      if (this.jsonError || !isRecord(this.value)) {
        this.jsonError =
          this.jsonError || "Enter a JSON object to use the form.";
        return;
      }
      // TaskForm restarts from its initial data, so hand it the JSON value.
      this.initial = structuredClone(this.value);
      this.formValid = true;
      this.jsonMode = false;
    } else {
      this.json = JSON.stringify(this.value ?? {}, null, 2);
      this.jsonError = "";
      this.jsonMode = true;
    }
  }
}
