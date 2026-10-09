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
// The step details registry, version 2: the one place step kinds, their
// parameter components and AI agent slots are registered. Step details, the
// canvas and the Add a step panel read it. Changing a type here changes the
// contract every registering module compiles against, so it needs version 3;
// version 2 added the declarative Parameters and Settings forms.
import type { Signal, Type } from "@angular/core";
import type { ReferenceScope, Schema } from "../../forms/core/scope";
import type { Step, Workflow } from "../../model";
import type { StepTestRequest } from "../state/execution";

export const NDV_REGISTRY_VERSION = 2 as const;
export type Json =
  | null
  | boolean
  | number
  | string
  | Json[]
  | { [key: string]: Json };
export type Path = (string | number)[];
/** One of literal | ref | object | array | op, plus forms the language adds. */
export type Expression = { [form: string]: unknown };
/**
 * An instance key: the step ID, then `[i]` per enclosing loop, `#seg.seg` for
 * repeated activations and `~n` for a loop's yields (`send[2]`, `support#2.1`).
 */
export type InstanceKey = string;

export type NodeRole =
  | "trigger"
  | "app"
  | "ai-task"
  | "ai-agent"
  | "rules"
  | "decision"
  | "parallel"
  | "loop"
  | "sub-workflow"
  | "transform"
  | "human"
  | "wait"
  | "signal"
  | "fail";
export type PanelCategory = "ai" | "app" | "data" | "flow" | "wait" | "human";

export interface KindContext {
  workflow: Workflow;
  /** The target's language features (`text.concat`, `flow.forEach`, …). */
  features: readonly string[];
  /** Published or local Action document. */
  actionContract(uses: string): Json | null;
  /** DecisionTable document. */
  tableContract(uses: string): Json | null;
  /** Callee interface for `callWorkflow`: {inputSchema, outputSchema, callable}, or null when unknown. */
  workflowContract(uses: string): Json | null;
  /** The recipe of an action this workflow owns (its method, path, timeout and retry), or null. */
  ownedAction?(uses: string): Json | null;
}
export interface FieldSpec {
  /** Relative to the step, e.g. ["with"], ["cases", 0, "when"]. */
  path: Path;
  label: string;
  templateCapable?: boolean;
  expectedSchema?(step: Step, ctx: KindContext): Schema | null;
}
export interface ContainerSpec {
  /** Where a nested step list lives: a manifest block path plus "steps", e.g. ["body", "steps"]. */
  path: Path;
  layout: "lanes" | "frame";
  /** Lane or frame header. */
  label(step: Step, ctx: KindContext): string;
}
export interface OutputHandle {
  id: string;
  label: string;
  containerPath?: Path;
}

/** What a Parameters or Settings tab shows: default fields, then options offered by Add option. */
export interface FormSpec {
  /** Shown by default, in order. */
  fields: ParamSpec[];
  /** Offered by Add option, in order; shown once added, or always while required and visible. */
  options?: ParamSpec[];
}
export type ParamType =
  | "text"
  | "multiline"
  | "number"
  | "duration"
  | "boolean"
  | "select"
  | "multiSelect"
  | "dateTime"
  | "json"
  | "list"
  | "keyValue"
  | "fields"
  | "conditions"
  | "resource"
  | "connection"
  | "model"
  | "schema"
  | "formula"
  | "fileRef"
  | "urlTemplate"
  | "notice"
  | "custom";
export interface Choice {
  value: Json;
  label: string;
  description?: string;
  /** Why the choice can't be used; it stays visible. */
  disabled?: string;
  group?: string;
}
/** One field of a form, drawn by the shared renderer. */
export interface ParamSpec {
  /** Stable within the kind: the `data-param` test hook and the diagnostics key. */
  id: string;
  /**
   * Where the value lives, relative to the step (default), the workflow, or the
   * workflow-owned action. A path that starts with one of the step's expression
   * fields continues as a data path inside that expression.
   */
  path: Path;
  scope?: "step" | "workflow" | "action";
  type: ParamType;
  /** A sentence-case noun phrase. */
  label: string;
  required?: boolean;
  /** The language default: a placeholder or a preselected choice. */
  default?: Json;
  /** An example value, when there is no default. */
  placeholder?: string;
  /** One short sentence under the field. */
  hint?: string;
  /** Longer help behind the "?" toggletip. */
  description?: string;
  /** "both": Fixed | Mapped (expression paths only); "fixed": structure; "mapped": references only. */
  mapping?: "both" | "fixed" | "mapped";
  /** Text fields that become templates with text.concat. */
  templateCapable?: boolean;
  choices?: Choice[] | ((ctx: NdvContext) => Promise<Choice[]>);
  min?: number;
  max?: number;
  /** list only: fewest and most items. */
  minItems?: number;
  maxItems?: number;
  units?: ("seconds" | "minutes" | "hours" | "days")[];
  maxLength?: number;
  /** Normalized on blur like step names. */
  identifier?: boolean;
  showWhen?(step: Step, ctx: KindContext): boolean;
  /** Why `showWhen` hides it now; Add option lists it disabled with this reason. */
  hiddenReason?(step: Step, ctx: KindContext): string;
  /** When showWhen turns false: "clear" (default) removes the stored value in the same undo step; "keep" leaves it. */
  whenHidden?: "clear" | "keep";
  /**
   * What Remove option and Reset to default write: "delete" (default) removes
   * the key; "default" writes the field's `default`, for a key the language
   * requires. The field then reads as not added because it holds its default.
   */
  whenRemoved?: "delete" | "default";
  /** list items; their paths are relative to the item. */
  item?: ParamSpec;
  /** fields rows and nested groups; their paths are absolute. */
  children?(step: Step, ctx: KindContext): ParamSpec[];
  /** "Add recipient", "Add header". */
  addLabel?: string;
  /** list only: chips for short identifier lists such as answers. */
  display?: "rows" | "chips";
  /** A manifest feature gate; disabled with the reason when missing. */
  feature?: string;
  /** The reason it is read-only, shown as the hint; null when editable. */
  readOnly?(step: Step, ctx: KindContext): string | null;
  /** type "custom" only: the subNode key of this kind's registerParameters() registration. */
  component?: string;
}

export interface StepKindDescriptor {
  /** The definition `kind`. */
  kind: string;
  /** Manifest feature that gates the kind, e.g. "flow.forEach". */
  feature?: string;
  label: string;
  description: string;
  keywords: string;
  /** A `<weave-icon>` name. */
  icon: string;
  role: NodeRole;
  category: PanelCategory;
  idPrefix: string;
  /** A new step with its defaults. */
  create(id: string): Step;
  /** Every expression field, for scope, references and stale marking. */
  fields(step: Step): FieldSpec[];
  containers?(step: Step): ContainerSpec[];
  /** Labeled outputs; one unlabeled output when absent. */
  outputs?(step: Step, ctx: KindContext): OutputHandle[];
  /** The tile subtitle. */
  summary(step: Step, ctx: KindContext): string;
  outputSchema(step: Step, ctx: KindContext): Schema | null;
  /** The simulator takes this step's output as a mock under `node:<instance key>` or `node:<id>`. */
  pinnable: boolean | ((step: Step) => boolean);
  /** Test data scripts the simulator needs for this kind. */
  script?: "signal" | "human" | "ai-turns";
  real?: RealExecutionSupport;
  /** The Parameters tab, drawn by the shared form renderer. */
  form?(step: Step, ctx: KindContext): FormSpec;
  /** Settings rows other than On error, Notes and About this step, which step details adds itself. */
  settings?(step: Step, ctx: KindContext): FormSpec;
}

export interface ParameterRegistration {
  kind: string;
  /** An AI agent slot ID, e.g. "model", "tools". */
  subNode?: string;
  wide?: boolean;
  load(): Promise<Type<ParameterComponent>>;
}
/** Implemented by the registered Angular component. */
export interface ParameterComponent {
  /** Provided with input.required(). */
  readonly context: Signal<NdvContext>;
}

export type DataSource =
  | "simulated"
  | "test-call"
  | "pinned"
  | "run"
  | "manual";
export type StaleReason = "settings" | "contract" | "upstream";
export type SampleValue =
  | { state: "none" }
  | {
      state: "available";
      value: Json;
      source: DataSource;
      stale: StaleReason | null;
    }
  | { state: "omitted"; reason: string };
export interface PreviewResult {
  ok: boolean;
  value?: Json;
  error?: { code: string; message: string };
}
export interface SampleContext {
  input: Json;
  steps: Record<string, { output: Json }>;
  /** The innermost loop's current item. */
  item?: Json;
  /** Its position, from 0. */
  index?: number;
  /** Every enclosing loop, by loop step ID. */
  loops?: Record<string, { item: Json; index: number }>;
}
export interface Edit {
  path: Path;
  /** undefined deletes the key. */
  value: Json | Expression | undefined;
  /** "step" (default): path is relative to the step. "workflow": relative to the workflow document. */
  scope?: "step" | "workflow";
}

export interface NdvContext {
  readonly version: 2;
  /** Immutable snapshot; edits go through edit(). */
  readonly step: Step;
  readonly workflow: Workflow;
  /** Run overlay, read-only source, locked editing. */
  readonly readOnly: boolean;
  /** The iteration picked in the Input pane; null outside loops. */
  readonly instanceKey: InstanceKey | null;
  /** null when working locally. */
  readonly environment: {
    id: string;
    name: string;
    testCalls: "enabled" | "disabled";
  } | null;
  scope(fieldPath: Path): ReferenceScope;
  expectedSchema(fieldPath: Path): Schema | null;
  outputSchema(): Schema | null;
  evaluate(expression: Expression, at: Path): PreviewResult;
  diagnostics(
    fieldPath?: Path,
  ): { code: string; severity: string; message: string; path: string }[];
  sample: {
    context(at: Path): SampleContext | null;
    resolvedInput(): SampleValue;
    output(): SampleValue;
    setPin(value: Json): Promise<{ ok: true } | { ok: false; message: string }>;
    clearPin(): void;
    /** The signal, human or AI turns entry for this step (or instance key). */
    script(): Json | null;
    setScript(
      value: Json | null,
    ): Promise<{ ok: true } | { ok: false; message: string }>;
  };
  catalog: {
    actions(filter?: {
      connector?: string;
    }): Promise<{ uses: string; title: string }[]>;
    contract(uses: string): Promise<Json | null>;
    /** Published workflows; with callableBy, those that don't allow that caller carry `disabled`. */
    workflows(filter?: {
      callableBy?: string;
    }): Promise<{ uses: string; title: string; disabled?: string }[]>;
    /** Published decision tables, newest version first. */
    tables(): Promise<{ uses: string; title: string }[]>;
  };
  connections: {
    slot(name: string): { connector: string; required: boolean } | null;
    devBinding(slot: string): {
      connectionId: string;
      name: string;
      tested: "ok" | "failed" | null;
    } | null;
    /** Opens choose or create, then tests the connection. */
    bind(slot: string): Promise<void>;
  };
  /** One undo step. A burst of edits inside 600 ms merges into it. */
  edit(changes: Edit[], label: string): void;
  openAddStep(request: { slot?: string; after?: string }): void;
  /** index selects one entry of a many-valued slot (Tools). */
  openSubNode(slot: string, index?: number): void;
}

/** A chip under an AI agent: data only; Studio draws it. */
export interface SubNodeChip {
  text: string;
  state: "ok" | "missing" | "error";
}
export interface SubNodeEntry {
  id: string;
  label: string;
  icon: string;
  /** e.g. "Actions", "HTTP requests", "Sub-workflows", "Ask a person". */
  group: string;
  /** Why the entry can't be used; the entry stays visible. */
  disabled?: string;
  apply(): void;
}
export interface SubNodeSlotSpec {
  /** "model" | "memory" | "tools" | "output" for the AI agent. */
  id: string;
  /** "Model", "Memory", "Tools", "Output". */
  label: string;
  required: boolean;
  max: number | null;
  chip(step: Step, ctx: KindContext): SubNodeChip;
  /** One chip per entry of a many-valued slot (Tools), in order; openSubNode(slot, index) opens one. */
  items?(step: Step, ctx: KindContext): SubNodeChip[];
  entries(ctx: NdvContext): Promise<SubNodeEntry[]>;
}
export interface RealExecutionSupport {
  /**
   * Body for POST {ENV}/step-tests, or why the step can't run in an
   * environment. The body is the whole request except `draft_id` and
   * `draft_revision`: the editor adds the saved draft's when it sends it.
   */
  request(
    ctx: NdvContext,
  ):
    | { body: Omit<StepTestRequest, "draft_id" | "draft_revision"> }
    | { blocked: string };
}

export class RegistryError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "RegistryError";
  }
}

const idPrefixPattern = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;
const parameterKey = (kind: string, subNode?: string) =>
  subNode ? `${kind}/${subNode}` : kind;

/** Registrations for one Studio session; `ndvRegistry` is the shared one. */
export class NdvRegistry {
  readonly version = NDV_REGISTRY_VERSION;
  private readonly descriptors = new Map<string, StepKindDescriptor>();
  private readonly components = new Map<string, ParameterRegistration>();
  private readonly slots = new Map<string, readonly SubNodeSlotSpec[]>();

  registerKind(descriptor: StepKindDescriptor): void {
    if (this.descriptors.has(descriptor.kind))
      throw new RegistryError(
        `Step kind "${descriptor.kind}" is already registered.`,
      );
    if (!idPrefixPattern.test(descriptor.idPrefix))
      throw new RegistryError(
        `Use letters, numbers, dots, underscores or hyphens in the ID prefix of "${descriptor.kind}".`,
      );
    this.descriptors.set(descriptor.kind, descriptor);
  }

  registerParameters(registration: ParameterRegistration): void {
    const key = parameterKey(registration.kind, registration.subNode);
    if (this.components.has(key))
      throw new RegistryError(
        `Parameters for "${key}" are already registered.`,
      );
    this.components.set(key, registration);
  }

  registerSubNodes(kind: string, slots: SubNodeSlotSpec[]): void {
    if (this.slots.has(kind))
      throw new RegistryError(`Slots for "${kind}" are already registered.`);
    const ids = new Set<string>();
    for (const slot of slots) {
      if (ids.has(slot.id))
        throw new RegistryError(
          `Slot "${slot.id}" is listed twice for "${kind}".`,
        );
      if (slot.max !== null && !(Number.isInteger(slot.max) && slot.max >= 1))
        throw new RegistryError(
          `Slot "${slot.id}" of "${kind}" needs a maximum of at least 1, or none.`,
        );
      ids.add(slot.id);
    }
    this.slots.set(kind, Object.freeze([...slots]));
  }

  kind(kind: string): StepKindDescriptor | undefined {
    return this.descriptors.get(kind);
  }
  /** Every kind, in registration order. */
  kinds(): StepKindDescriptor[] {
    return [...this.descriptors.values()];
  }
  parameters(
    kind: string,
    subNode?: string,
  ): ParameterRegistration | undefined {
    return this.components.get(parameterKey(kind, subNode));
  }
  subNodes(kind: string): readonly SubNodeSlotSpec[] {
    return this.slots.get(kind) ?? [];
  }
}

export const ndvRegistry = new NdvRegistry();
export function registerKind(descriptor: StepKindDescriptor): void {
  ndvRegistry.registerKind(descriptor);
}
export function registerParameters(registration: ParameterRegistration): void {
  ndvRegistry.registerParameters(registration);
}
export function registerSubNodes(kind: string, slots: SubNodeSlotSpec[]): void {
  ndvRegistry.registerSubNodes(kind, slots);
}
