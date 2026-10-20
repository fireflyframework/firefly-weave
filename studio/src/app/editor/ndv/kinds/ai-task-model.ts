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
// The AI task, connection first: one model choice writes the step's AI
// settings (`llmProfiles["<step>"]`), the workflow's AI connection slot and
// the step's action, and the canvas asks for a model instead of showing the
// compiler's errors about the settings and the slot that don't exist yet.
// Nothing here talks to the platform; the model field loads the lists.
import type {
  BindableConnection,
  AiModel,
} from "../../../integrations/ai-setup-api";
import type { SelectOption } from "../../../forms/ui/select";
import type { Step, Workflow } from "../../../model";
import { FormWriteError, type FormChange } from "../params/value-io";

export const AI_CONNECTOR = "weave-agentic-provider@1.0.0";
export const AI_ACTION = "weave-agentic-generate@1.0.0";
export const AI_SLOT = "ai";
/** A new AI task answers in text. */
export const TEXT_RESULT = { type: "string", maxLength: 20000 } as const;
export const DEFAULT_MAX_TOKENS = 1024;
/** The language keeps at most this many sets of AI settings in one workflow. */
export const MAX_PROFILES = 32;
export const SETUP = "Choose a model";
const TOO_MANY =
  "A workflow holds at most 32 sets of AI settings. Share the settings of another AI step instead.";
/** Separates a connection revision and a model name in one select value. */
const SEPARATOR = "\u0000";

type Record_ = Record<string, unknown>;
const isRecord = (value: unknown): value is Record_ =>
  !!value && typeof value === "object" && !Array.isArray(value);

export function profilesOf(workflow: Workflow): Record_ {
  const profiles = workflow.spec["llmProfiles"];
  return isRecord(profiles) ? profiles : {};
}
/** The settings an AI task reads: its own, or those it shares. */
export function profileNameOf(step: Step): string {
  return typeof step["profile"] === "string" ? step["profile"] : "";
}
export function profileOf(workflow: Workflow, step: Step): Record_ | null {
  const profile = profilesOf(workflow)[profileNameOf(step)];
  return isRecord(profile) ? profile : null;
}
/** The AI tasks that use one set of settings, in workflow order. */
export function stepsSharing(workflow: Workflow, name: string): string[] {
  const found: string[] = [];
  const visit = (value: unknown): void => {
    if (Array.isArray(value)) value.forEach(visit);
    else if (isRecord(value)) {
      if (value["kind"] === "llm" && value["profile"] === name)
        found.push(String(value["id"]));
      Object.values(value).forEach(visit);
    }
  };
  visit(workflow.spec.steps);
  return found;
}

const TEXT_KEYWORDS = new Set([
  "type",
  "minLength",
  "maxLength",
  "title",
  "description",
]);
/** A plain string result, which the worker asks for as text; the same rule as the worker's. */
export function isTextResult(schema: unknown): boolean {
  return (
    isRecord(schema) &&
    schema["type"] === "string" &&
    Object.keys(schema).every((key) => TEXT_KEYWORDS.has(key))
  );
}

/** What an AI task still needs on the canvas: a model, until its settings exist. */
export function aiTaskSetup(workflow: Workflow, step: Step): string {
  return step.kind === "llm" && !profileOf(workflow, step) ? SETUP : "";
}
/**
 * True while an AI task has no model: the canvas lists the compiler's
 * messages about it (its missing settings and slot, an empty prompt) as the
 * setup it needs, under "Choose a model", instead of as errors.
 */
export function deferredToSetup(workflow: Workflow, stepId: string): boolean {
  const step = findStep(workflow.spec.steps, stepId);
  return !!step && !!aiTaskSetup(workflow, step);
}
function findStep(value: unknown, id: string): Step | null {
  if (Array.isArray(value)) {
    for (const item of value) {
      const found = findStep(item, id);
      if (found) return found;
    }
  } else if (isRecord(value)) {
    if (value["id"] === id && typeof value["kind"] === "string")
      return value as Step;
    for (const child of Object.values(value)) {
      const found = findStep(child, id);
      if (found) return found;
    }
  }
  return null;
}

/** One model on one AI connection. */
export interface ModelChoice {
  connectionId: string;
  revisionId: string;
  /** The connection's name. */
  connection: string;
  provider: string;
  model: string;
}
/** Which connection a slot is bound to while editing, or null. */
export type BoundTo = (slot: string) => string | null;

const slotName = (connection: string): string =>
  `${AI_SLOT}-${connection.toLowerCase().replace(/[^a-z0-9_.-]+/g, "-")}`.slice(
    0,
    63,
  );

/**
 * The slot an AI task uses for a connection: `ai`, unless `ai` is bound to
 * another connection while editing; then `ai-<connection name>`.
 */
export function slotFor(
  workflow: Workflow,
  choice: ModelChoice,
  boundTo: BoundTo = () => null,
): string {
  const bound = boundTo(AI_SLOT);
  if (!bound || bound === choice.connectionId) return AI_SLOT;
  return slotName(choice.connection);
}

function freeName(profiles: Record_, id: string): string {
  let name = id;
  for (let n = 2; name in profiles; n++) name = `${id.slice(0, 60)}-${n}`;
  return name;
}

/** The changes one model choice makes, as one undo step. */
export function chooseModel(
  workflow: Workflow,
  step: Step,
  choice: ModelChoice,
  boundTo: BoundTo = () => null,
): FormChange[] {
  const current = profileOf(workflow, step);
  const profiles = profilesOf(workflow);
  // A step without settings gets its own, named after it (another name when taken).
  const name = current ? profileNameOf(step) : freeName(profiles, step.id);
  if (!current && Object.keys(profiles).length >= MAX_PROFILES)
    throw new FormWriteError(TOO_MANY);
  const profile = current
    ? { ...current, provider: choice.provider, model: choice.model }
    : {
        provider: choice.provider,
        model: choice.model,
        options: { max_tokens: DEFAULT_MAX_TOKENS },
        outputSchema: { ...TEXT_RESULT },
      };
  const slot = slotFor(workflow, choice, boundTo);
  const slots = isRecord(workflow.spec["connections"])
    ? workflow.spec["connections"]
    : {};
  return [
    { scope: "workflow", path: ["spec", "llmProfiles", name], value: profile },
    ...(isRecord(slots[slot])
      ? []
      : [
          {
            scope: "workflow" as const,
            path: ["spec", "connections", slot],
            value: { connector: AI_CONNECTOR, required: true },
          },
        ]),
    { scope: "step", path: ["profile"], value: name },
    { scope: "step", path: ["connection"], value: slot },
    { scope: "step", path: ["uses"], value: AI_ACTION },
  ];
}

/** A connection and the models it serves, or why they couldn't be listed. */
export interface ConnectionModels {
  connection: BindableConnection;
  models: AiModel[] | null;
  error?: string;
}
export const modelValue = (revisionId: string, model: string): string =>
  `${revisionId}${SEPARATOR}${model}`;
export function parseModelValue(
  value: string,
): { revisionId: string; model: string } | null {
  const at = value.indexOf(SEPARATOR);
  return at > 0 && at < value.length - 1
    ? { revisionId: value.slice(0, at), model: value.slice(at + 1) }
    : null;
}
const local = (connection: BindableConnection): boolean =>
  (connection.endpoint_origin ?? "").startsWith("http://");

/** What a model can do, in the order the field shows it. */
export function modelFacts(
  model: AiModel,
  connection: BindableConnection,
): string[] {
  return [
    ...(model.tools === "yes" ? ["Calls tools"] : []),
    ...(model.context_tokens
      ? [`${model.context_tokens.toLocaleString("en-US")} tokens of context`]
      : []),
    ...(local(connection) ? ["Local"] : []),
  ];
}
/** The model field's choices: approved models grouped by AI connection. */
export function modelOptions(
  groups: readonly ConnectionModels[],
): SelectOption[] {
  return groups.flatMap(({ connection, models, error }) => {
    const group = connection.label;
    if (!models)
      return [
        {
          value: modelValue(connection.revision_id, ""),
          label: "Models unavailable",
          description:
            error ?? "Studio couldn't list this connection's models.",
          group,
          disabled: true,
        },
      ];
    return models
      .filter((model) => model.available)
      .map((model) =>
        model.approved
          ? {
              value: modelValue(connection.revision_id, model.name),
              label: model.name,
              description: modelFacts(model, connection).join(" · "),
              group,
            }
          : {
              value: modelValue(connection.revision_id, model.name),
              label: model.name,
              description:
                "Not approved for this connection. Approve it in Settings › AI models.",
              group,
              disabled: true,
            },
      );
  });
}
/**
 * The listed choice that matches the step's settings: on the connection its
 * slot is bound to, else on the only connection that serves the model.
 */
export function currentModelValue(
  workflow: Workflow,
  step: Step,
  groups: readonly ConnectionModels[],
  boundTo: BoundTo = () => null,
): string {
  const profile = profileOf(workflow, step);
  if (!profile) return "";
  const serving = groups.filter(
    ({ connection, models }) =>
      connection.provider === profile["provider"] &&
      !!models?.some((model) => model.name === profile["model"]),
  );
  const bound = boundTo(String(step["connection"] ?? AI_SLOT));
  const found = bound
    ? serving.find(({ connection }) => connection.connection_id === bound)
    : serving.length === 1
      ? serving[0]
      : undefined;
  return found
    ? modelValue(found.connection.revision_id, String(profile["model"]))
    : "";
}

// ------------------------------------------------- recorded AI turns
// The same rules as urn:firefly-weave:schema:studio-ai-turns:v1
// (src/firefly_weave/contracts/schemas/studio-ai-turns-v1.json); a vitest
// suite checks them against the platform's validator.
const ENTRY_KEYS = new Set([
  "turns",
  "memory",
  "autoApprove",
  "source",
  "runId",
  "runEnvironmentId",
  "configHash",
  "upstreamHash",
]);
const SOURCES = new Set(["manual", "simulated", "test-call", "run"]);
const HASH = /^fnv1a64:[0-9a-f]{16}$/;
const UUID =
  /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;
const only = (value: Record_, keys: string[]) =>
  Object.keys(value).every((key) => keys.includes(key));

function turnProblem(turn: unknown): boolean {
  if (!isRecord(turn)) return true;
  if ("text" in turn && typeof turn["text"] !== "string") return true;
  if ("final" in turn) return !only(turn, ["final", "text"]);
  const calls = turn["toolCalls"];
  return (
    !only(turn, ["toolCalls", "text"]) ||
    !Array.isArray(calls) ||
    calls.length < 1 ||
    calls.length > 16 ||
    calls.some(
      (call) =>
        !isRecord(call) ||
        !only(call, ["name", "arguments"]) ||
        typeof call["name"] !== "string" ||
        !isRecord(call["arguments"]),
    )
  );
}
/** Where a recorded AI turns entry breaks its schema (a JSON pointer, "" for the entry), or null. */
export function aiTurnsProblem(entry: unknown): string | null {
  if (!isRecord(entry) || !only(entry, [...ENTRY_KEYS])) return "";
  const turns = entry["turns"];
  if (!Array.isArray(turns) || turns.length > 33) return "/turns";
  const bad = turns.findIndex(turnProblem);
  if (bad >= 0) return `/turns/${bad}`;
  const memory = entry["memory"];
  if (memory !== undefined) {
    if (!Array.isArray(memory) || memory.length > 200) return "/memory";
    const wrong = memory.findIndex(
      (message) =>
        !isRecord(message) ||
        !only(message, ["role", "content"]) ||
        !("content" in message) ||
        (message["role"] !== "user" && message["role"] !== "assistant"),
    );
    if (wrong >= 0) return `/memory/${wrong}`;
  }
  if ("autoApprove" in entry && typeof entry["autoApprove"] !== "boolean")
    return "/autoApprove";
  if ("source" in entry && !SOURCES.has(entry["source"] as string))
    return "/source";
  for (const key of ["runId", "runEnvironmentId"])
    if (
      key in entry &&
      !(typeof entry[key] === "string" && UUID.test(entry[key] as string))
    )
      return `/${key}`;
  for (const key of ["configHash", "upstreamHash"])
    if (
      key in entry &&
      !(typeof entry[key] === "string" && HASH.test(entry[key] as string))
    )
      return `/${key}`;
  const fromRun = entry["source"] === "run" || entry["source"] === "test-call";
  if (fromRun && !("runId" in entry && "runEnvironmentId" in entry)) return "";
  if ("runId" in entry && !("source" in entry && "runEnvironmentId" in entry))
    return "";
  return null;
}
