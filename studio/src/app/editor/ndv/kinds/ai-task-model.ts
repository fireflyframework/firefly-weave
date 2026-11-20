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
import { canonicalJson, type Json } from "../../../forms/core/json";
import type { SelectOption } from "../../../forms/ui/select";
import type { Step, Workflow } from "../../../model";
import type { SideEffect, StepTestRequest } from "../../state/execution";
import type { AiTurnsEntry } from "../../state/test-data";
import { FormWriteError, type FormChange } from "../params/value-io";
import type { NdvContext, RealExecutionSupport } from "../registry";
import {
  isRecord,
  profileNameOf,
  profileOf,
  profilesOf,
  type Record_,
} from "./ai-task-setup";

export {
  SETUP,
  aiTaskSetup,
  deferredToSetup,
  profileNameOf,
  profileOf,
  profilesOf,
} from "./ai-task-setup";

export const AI_CONNECTOR = "weave-agentic-provider@1.0.0";
export const AI_ACTION = "weave-agentic-generate@1.0.0";
export const AI_SLOT = "ai";
/** A new AI task answers in text. */
export const TEXT_RESULT = { type: "string", maxLength: 20000 } as const;
export const DEFAULT_MAX_TOKENS = 1024;
/** The language keeps at most this many sets of AI settings in one workflow. */
export const MAX_PROFILES = 32;
const TOO_MANY =
  "A workflow holds at most 32 sets of AI settings. Share the settings of another AI step instead.";
/** Separates a connection revision and a model name in one select value. */
const SEPARATOR = "\u0000";

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
 * another connection while editing; then `ai-<connection name>`. A slot the
 * workflow already declares for another connector is never reused: the next
 * free `ai-<connection name>`, `ai-<connection name>-2`… is taken instead.
 */
export function slotFor(
  workflow: Workflow,
  choice: ModelChoice,
  boundTo: BoundTo = () => null,
): string {
  const slots = isRecord(workflow.spec["connections"])
    ? workflow.spec["connections"]
    : {};
  const usable = (name: string) => {
    const slot = slots[name];
    return !isRecord(slot) || slot["connector"] === AI_CONNECTOR;
  };
  const bound = boundTo(AI_SLOT);
  if ((!bound || bound === choice.connectionId) && usable(AI_SLOT))
    return AI_SLOT;
  const base = slotName(choice.connection);
  let name = base;
  for (let n = 2; !usable(name); n++) name = `${base.slice(0, 60)}-${n}`;
  return name;
}

function freeName(profiles: Record_, id: string): string {
  let name = id;
  for (let n = 2; name in profiles; n++) name = `${id.slice(0, 60)}-${n}`;
  return name;
}

/** The settings a new AI task points at until a model is chosen. */
export const DEFAULT_PROFILE = "default";

/** Whether the step's settings exist and no other AI step points at them. */
export function ownsSettings(workflow: Workflow, step: Step): boolean {
  return (
    !!profileOf(workflow, step) &&
    stepsSharing(workflow, profileNameOf(step)).every((id) => id === step.id)
  );
}
/**
 * What "This step only" points the step at: its own settings, whatever their
 * name; a step without settings keeps its name; a step that shares another
 * step's settings gets a free name after it (a model is then chosen again).
 */
export function ownSettingsName(workflow: Workflow, step: Step): string {
  if (!profileOf(workflow, step) || ownsSettings(workflow, step))
    return profileNameOf(step);
  return freeName(profilesOf(workflow), step.id);
}

/** What the Model field says when other AI steps use the same settings, or "". */
export function sharingNote(workflow: Workflow, step: Step): string {
  if (!profileOf(workflow, step)) return "";
  const others = stepsSharing(workflow, profileNameOf(step)).filter(
    (id) => id !== step.id,
  ).length;
  if (!others) return "";
  const steps = others === 1 ? "1 other AI step" : `${others} other AI steps`;
  return profileNameOf(step) === DEFAULT_PROFILE
    ? `Uses the default settings, like ${steps}. Choosing a model gives this step its own.`
    : `Shared with ${steps}. A new model changes theirs too.`;
}

/** The changes one model choice makes, as one undo step. */
export function chooseModel(
  workflow: Workflow,
  step: Step,
  choice: ModelChoice,
  boundTo: BoundTo = () => null,
): FormChange[] {
  const profiles = profilesOf(workflow);
  // Settings named default that other AI steps also use are where a new AI
  // task starts, not a choice to share them: the step gets its own instead.
  const startedShared =
    profileNameOf(step) === DEFAULT_PROFILE && !ownsSettings(workflow, step);
  const current = startedShared ? null : profileOf(workflow, step);
  // A step without settings gets its own, named after it (another name when taken).
  const name = current ? profileNameOf(step) : freeName(profiles, step.id);
  if (!current && Object.keys(profiles).length >= MAX_PROFILES)
    throw new FormWriteError(TOO_MANY);
  if (!choice.provider)
    throw new FormWriteError(
      "This AI connection doesn't name its provider. Edit it in Settings › AI models.",
    );
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
// "Local" means a plain HTTP endpoint: models served that way run beside the
// platform (Ollama on the host or in Compose), while cloud endpoints use HTTPS.
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
    const unusable = !connection.provider
      ? "This AI connection doesn't name its provider. Edit it in Settings › AI models."
      : !models
        ? (error ?? "Studio couldn't list this connection's models.")
        : "";
    if (unusable)
      return [
        {
          value: modelValue(connection.revision_id, ""),
          label: "Models unavailable",
          description: unusable,
          group,
          disabled: true,
        },
      ];
    if (!models) return [];
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
 * The listed choice that matches the step's settings: a connection where the
 * model is approved and available, preferring the one its slot is bound to.
 */
export function currentModelValue(
  workflow: Workflow,
  step: Step,
  groups: readonly ConnectionModels[],
  boundTo: BoundTo = () => null,
  chosen = "",
): string {
  const profile = profileOf(workflow, step);
  if (!profile) return "";
  const serving = groups.filter(
    ({ connection, models }) =>
      connection.provider === profile["provider"] &&
      !!models?.some(
        (model) =>
          model.name === profile["model"] && model.approved && model.available,
      ),
  );
  // The bound connection; else the one chosen in this field (`chosen` is its
  // revision); else the first connection that serves the model.
  const bound = boundTo(String(step["connection"] ?? AI_SLOT));
  const found = bound
    ? serving.find(({ connection }) => connection.connection_id === bound)
    : (serving.find(({ connection }) => connection.revision_id === chosen) ??
      serving[0]);
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

// ------------------------------------------------- saved turns, usage and step tests
export const CHOOSE_MODEL = "Choose a model first.";
export const BIND_AI = "Bind this step's AI connection first.";
export const RESULT_MISMATCH = "The sample doesn't match the result fields.";
/** What a replayed turn used: no model call. */
export const NO_USAGE = { requests: 0, inputTokens: 0, outputTokens: 0 };
/** An AI task's timeout when its settings name none. */
export const DEFAULT_TIMEOUT_SECONDS = 120;
/** The platform runs a step test for at most five minutes. */
export const STEP_TEST_MAX_SECONDS = 300;

/** Where a saved turn came from, with the run a real answer came from. */
export type TurnProvenance =
  | { source: "simulated" | "manual" }
  | { source: "test-call" | "run"; runId: string; runEnvironmentId: string };

const TYPES: Record<string, (value: unknown) => boolean> = {
  string: (value) => typeof value === "string",
  object: isRecord,
  array: Array.isArray,
  number: (value) => typeof value === "number",
  integer: (value) => Number.isInteger(value),
  boolean: (value) => typeof value === "boolean",
  null: (value) => value === null,
};
/**
 * Whether a value has the type the result fields name. Only the type: the
 * Studio host checks the whole schema when it replays the turn.
 */
function fitsType(schema: unknown, value: unknown): boolean {
  if (!isRecord(schema)) return true;
  const type = schema["type"];
  const types =
    typeof type === "string" ? [type] : Array.isArray(type) ? type : null;
  return !types || types.some((name) => TYPES[String(name)]?.(value) ?? true);
}
const modelOf = (profile: Record_ | null): string =>
  typeof profile?.["model"] === "string" ? profile["model"] : "";

/** The result fields of an AI task that has a model (text when none are set); null without a model. */
export function resultSchemaOf(workflow: Workflow, step: Step): Json | null {
  const profile = profileOf(workflow, step);
  if (!modelOf(profile)) return null;
  return (profile?.["outputSchema"] as Json | undefined) ?? { ...TEXT_RESULT };
}

/** The saved turn an AI task replays: one final answer, with where it came from. */
export function aiTaskTurn(
  workflow: Workflow,
  step: Step,
  value: Json,
  provenance: TurnProvenance,
): { entry: AiTurnsEntry } | { problem: string } {
  const profile = profileOf(workflow, step);
  if (!modelOf(profile)) return { problem: CHOOSE_MODEL };
  if (!fitsType(profile?.["outputSchema"], value))
    return { problem: RESULT_MISMATCH };
  const entry: AiTurnsEntry = { turns: [{ final: value }], ...provenance };
  return aiTurnsProblem(entry) === null
    ? { entry }
    : { problem: RESULT_MISMATCH };
}

/**
 * What a simulated AI task returns for a saved entry, as the Studio host's
 * simulator answers: the final answer, no usage, the step's provider and
 * model. Undefined when the entry isn't one final turn.
 */
export function aiTaskReplay(
  workflow: Workflow,
  step: Step,
  entry: unknown,
): Json | undefined {
  const profile = profileOf(workflow, step);
  if (!modelOf(profile) || aiTurnsProblem(entry) !== null) return undefined;
  const turns = (entry as { turns: Record_[] }).turns;
  if (turns.length !== 1 || !("final" in turns[0])) return undefined;
  return {
    result: turns[0]["final"] as Json,
    usage: { ...NO_USAGE },
    provider: String(profile?.["provider"] ?? ""),
    model: modelOf(profile),
  };
}

const count = (value: unknown): value is number =>
  Number.isInteger(value) && (value as number) >= 0;
const plural = (n: number, word: string) =>
  `${n.toLocaleString("en-US")} ${word}${n === 1 ? "" : "s"}`;
/**
 * "1 request · 812 in · 64 out tokens" for an AI task's output, or what a
 * replay did when no model was called; null without usage counts.
 */
export function usageLine(output: unknown): string | null {
  const usage = isRecord(output) ? output["usage"] : null;
  if (!isRecord(usage)) return null;
  const { requests, inputTokens, outputTokens } = usage;
  if (!count(requests) || !count(inputTokens) || !count(outputTokens))
    return null;
  if (requests === 0) return "No model call · replayed the saved answer";
  return `${plural(requests, "request")} · ${inputTokens.toLocaleString("en-US")} in · ${plural(outputTokens, "out token")}`;
}

/**
 * Generate sample's answer for a text result: the result fields' first
 * example, else a sentence naming the step. Null for structured results,
 * whose sample comes from their fields.
 */
export function textSample(workflow: Workflow, step: Step): Json | null {
  const schema = resultSchemaOf(workflow, step);
  // A plain string; a constrained one (a list of values, a pattern, a format)
  // gets a sample that meets its constraint from its fields instead.
  if (
    !isRecord(schema) ||
    schema["type"] !== "string" ||
    ["enum", "const", "pattern", "format"].some((key) => key in schema)
  )
    return null;
  const examples = schema["examples"];
  return Array.isArray(examples) && typeof examples[0] === "string"
    ? examples[0]
    : `Sample answer from ${step.id}.`;
}

/**
 * Whether a saved turn holds the answer shown: it came from the same run, or
 * its final answer is the shown result (compared by value).
 */
export function holdsAnswer(
  entry: unknown,
  shown: unknown,
  runId: string | null,
): boolean {
  if (!isRecord(entry) || !isRecord(shown)) return false;
  if (runId && entry["runId"] === runId) return true;
  const turns = entry["turns"];
  const first = Array.isArray(turns) ? turns[0] : undefined;
  return (
    isRecord(first) &&
    "final" in first &&
    "result" in shown &&
    canonicalJson(first["final"]) === canonicalJson(shown["result"])
  );
}

const slotOf = (step: Step): string =>
  typeof step["connection"] === "string" && step["connection"]
    ? step["connection"]
    : AI_SLOT;

/** The step test of an AI task, without `draft_id` and `draft_revision`, which the editor adds. */
export function stepTestBody(
  workflow: Workflow,
  step: Step,
  revisionId: string,
  input: { prompt: Json; context: Json },
): Omit<StepTestRequest, "draft_id" | "draft_revision"> {
  const timeout = profileOf(workflow, step)?.["timeoutSeconds"];
  const seconds =
    typeof timeout === "number" && timeout > 0
      ? Math.ceil(timeout)
      : DEFAULT_TIMEOUT_SECONDS;
  return {
    kind: "llm",
    step_id: step.id,
    uses: AI_ACTION,
    profile: profileNameOf(step),
    input,
    connections: { [slotOf(step)]: revisionId },
    timeout_seconds: Math.min(seconds, STEP_TEST_MAX_SECONDS),
  };
}

/**
 * The AI connections Studio listed in this session, by revision: their label
 * and whether they're local. The bindable list names no credential, so a
 * connection served over plain HTTP (beside the platform) counts as local and
 * free; any other, or one never listed, may bill.
 */
const listed = new Map<string, { label: string; local: boolean }>();
/** "OpenAI (openai-prod)": the provider, then the connection's name, from a label "OpenAI · openai-prod". */
function connectionWords(connection: BindableConnection): string {
  const { label, name } = connection;
  const suffix = ` · ${name}`;
  if (label && label.endsWith(suffix) && label.length > suffix.length)
    return `${label.slice(0, -suffix.length)} (${name})`;
  return label && label !== name ? `${label} (${name})` : name;
}
export function rememberConnections(
  connections: readonly BindableConnection[],
): void {
  for (const connection of connections)
    listed.set(connection.revision_id, {
      label: connectionWords(connection),
      local: local(connection),
    });
}
/** What keeps a slot's binding on this computer (the connections port). */
export interface BindingStore {
  setBinding(
    workflow: string,
    slot: string,
    value: { connectionId: string; revisionId: string; name: string },
  ): Promise<boolean>;
}
/**
 * Keeps the connection chosen with a model as its slot's binding on this
 * computer, for Execute in an environment; the sentence to show when it
 * couldn't be kept (a refused or failed write), else null.
 */
export async function bindChosen(
  store: BindingStore,
  workflow: string,
  slot: string,
  connection: Pick<
    BindableConnection,
    "connection_id" | "revision_id" | "name"
  >,
): Promise<string | null> {
  const unkept = `${connection.name} is the connection for slot ${slot} for now, but Studio couldn't keep the choice on this computer.`;
  try {
    const kept = await store.setBinding(workflow, slot, {
      connectionId: connection.connection_id,
      revisionId: connection.revision_id,
      name: connection.name,
    });
    return kept ? null : unkept;
  } catch {
    return unkept;
  }
}

/** The cost sentence before a step test on a connection that may bill; null for a local one. */
export function costNotice(
  model: string,
  revisionId: string,
  name: string,
): string | null {
  const known = listed.get(revisionId);
  if (known?.local) return null;
  return `This calls ${model} on ${known?.label ?? name} and may incur cost.`;
}

/**
 * Execute in an environment for an AI task: the prompt and context worked out
 * from the data step details has, sent with the step's settings to the
 * connection bound to its slot.
 */
export function aiTaskRequest(
  ctx: NdvContext,
): ReturnType<RealExecutionSupport["request"]> {
  const { workflow, step } = ctx;
  const model = modelOf(profileOf(workflow, step));
  if (!model) return { blocked: CHOOSE_MODEL };
  const bound = ctx.connections.devBinding(slotOf(step));
  if (!bound) return { blocked: BIND_AI };
  const read = (field: "prompt" | "context") => {
    const expression = step[field];
    return isRecord(expression)
      ? ctx.evaluate(expression, [field])
      : { ok: true, value: null };
  };
  const prompt = read("prompt");
  const context = read("context");
  if (!prompt.ok || !context.ok)
    return {
      blocked: `${step.id} needs data from earlier steps. Execute previous steps first.`,
    };
  const body = stepTestBody(workflow, step, bound.revisionId, {
    prompt: (prompt.value ?? null) as Json,
    context: (context.value ?? null) as Json,
  });
  const confirm = costNotice(model, bound.revisionId, bound.name);
  // A local model call costs nothing and changes nothing: the step accepts
  // the action's effect itself, so step details doesn't ask.
  return confirm
    ? { body, confirm }
    : { body: { ...body, acknowledged_side_effect: AI_EFFECT } };
}

/** The AI action's declared effect: every call is a new model request (it isn't repeated). */
export const AI_EFFECT: SideEffect = "non_idempotent";
const isAiAction = (body: StepTestBody): boolean =>
  body.kind === "llm" && body.uses === AI_ACTION;
type StepTestBody = Omit<StepTestRequest, "draft_id" | "draft_revision">;

/**
 * The effect a step test acknowledges without asking, or null to ask: only an
 * AI task's call of the AI action on a local connection (its kind says so in
 * the body and has no cost to name), and only for that action's own effect.
 * `effect` is the action's as Studio read it; null when not loaded yet.
 */
export function withoutAsking(
  request: { body: StepTestBody; confirm?: string },
  effect: SideEffect | null,
): SideEffect | null {
  return !request.confirm &&
    isAiAction(request.body) &&
    request.body.acknowledged_side_effect === AI_EFFECT &&
    (effect ?? AI_EFFECT) === AI_EFFECT
    ? AI_EFFECT
    : null;
}
/** The effect a confirmed step test acknowledges: the action's, or the AI action's own when not loaded yet. */
export function effectToAcknowledge(
  body: StepTestBody,
  effect: SideEffect | null,
): SideEffect | null {
  return effect ?? (isAiAction(body) ? AI_EFFECT : null);
}
