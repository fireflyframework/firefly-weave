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
import "@angular/compiler";
import { beforeAll, describe, expect, it } from "vitest";
import { loadKindRegistrations } from "../src/app/editor/ndv/kinds";
import {
  TEXT_RESULT,
  aiTaskReplay,
  aiTaskTurn,
  bindChosen,
  effectToAcknowledge,
  holdsAnswer,
  rememberConnections,
  stepTestBody,
  usageLine,
  withoutAsking,
} from "../src/app/editor/ndv/kinds/ai-task-model";
import { formEnv, stepSubject } from "../src/app/editor/ndv/params/form-env";
import { formState } from "../src/app/editor/ndv/params/form-model";
import { designerSchema } from "../src/app/editor/ndv/params/schema-params";
import {
  ndvRegistry,
  type KindContext,
  type NdvContext,
} from "../src/app/editor/ndv/registry";
import {
  StepDetailsController,
  type ControllerHost,
} from "../src/app/editor/ndv/step-details-controller";
import type { StoreBackend } from "../src/app/editor/state/browser-store";
import type { LastResults } from "../src/app/editor/state/last-results";
import { TestDataService } from "../src/app/editor/state/test-data-service";
import {
  StructuredCanvasAdapter,
  createStep,
  freshWorkflow,
  type Step,
  type Workflow,
} from "../src/app/model";

beforeAll(() => loadKindRegistrations());

const context = (workflow: Workflow): KindContext => ({
  workflow,
  features: [],
  actionContract: () => null,
  tableContract: () => null,
  workflowContract: () => null,
});
const settings = {
  provider: "openai-chat",
  model: "qwen3:4b",
  options: { max_tokens: 1024 },
  outputSchema: TEXT_RESULT,
};
function task(
  chosen: boolean,
  profiles: Record<string, unknown> = {},
): { workflow: Workflow; step: Step } {
  const workflow = freshWorkflow();
  const step = {
    ...createStep("llm", "summarize"),
    ...(chosen ? { profile: "summarize" } : {}),
  };
  workflow.spec.steps.push(step);
  workflow.spec["llmProfiles"] = {
    ...(chosen ? { summarize: settings } : {}),
    ...profiles,
  };
  return { workflow, step };
}
function state(
  form: "form" | "settings",
  { workflow, step }: { workflow: Workflow; step: Step },
) {
  const descriptor = ndvRegistry.kind("llm")!;
  const ctx = context(workflow);
  return formState(
    descriptor[form]!(step, ctx),
    formEnv(stepSubject(descriptor, step, ctx, null), step, ctx),
  );
}

describe("the AI task's parameters", () => {
  it("shows exactly Model and Prompt on a new AI task, both required", () => {
    const { fields } = state("form", task(false));
    expect(
      fields.map((f) => [f.spec.label, f.spec.type, !!f.spec.required]),
    ).toEqual([
      ["Model", "model", true],
      ["Prompt", "multiline", true],
    ]);
    expect(fields[0].spec).toMatchObject({
      scope: "workflow",
      path: ["spec", "llmProfiles", "default", "model"],
      hint: "Models are grouped by AI connection. The connection becomes the slot the step uses.",
    });
    expect(fields[1].spec).toMatchObject({
      path: ["prompt"],
      mapping: "both",
      templateCapable: true,
      placeholder: "Summarize {{ the ticket text }} in two sentences.",
    });
  });
  it("waits for a model before offering the options that tune it", () => {
    expect(
      state("form", task(false)).addable.map((o) => [o.spec.label, o.disabled]),
    ).toEqual([
      ["Context", null],
      ["Structured result", "Choose a model first."],
      ["Max output tokens", "Choose a model first."],
      ["Temperature", "Choose a model first."],
      ["Reasoning pattern", "Choose a model first."],
      ["Share settings with other AI steps", null],
    ]);
  });
  it("tunes the step's own settings once a model is chosen", () => {
    const { addable } = state("form", task(true));
    expect(addable.every((o) => o.disabled === null)).toBe(true);
    const paths = Object.fromEntries(
      addable.map((o) => [o.spec.id, [o.spec.scope ?? "step", ...o.spec.path]]),
    );
    expect(paths).toEqual({
      context: ["step", "context"],
      result: ["workflow", "spec", "llmProfiles", "summarize", "outputSchema"],
      maxTokens: [
        "workflow",
        "spec",
        "llmProfiles",
        "summarize",
        "options",
        "max_tokens",
      ],
      temperature: [
        "workflow",
        "spec",
        "llmProfiles",
        "summarize",
        "options",
        "temperature",
      ],
      reasoning: [
        "workflow",
        "spec",
        "llmProfiles",
        "summarize",
        "reasoning",
        "pattern",
      ],
      share: ["step", "profile"],
    });
    const byId = Object.fromEntries(addable.map((o) => [o.spec.id, o.spec]));
    expect(byId["result"]).toMatchObject({
      type: "schema",
      default: TEXT_RESULT,
      whenRemoved: "default",
    });
    expect(byId["maxTokens"]).toMatchObject({
      default: 1024,
      min: 1,
      max: 32768,
      whenRemoved: "default",
    });
    expect(byId["temperature"]).toMatchObject({
      min: 0,
      max: 2,
      placeholder: "Model default",
    });
    expect(byId["temperature"].default).toBeUndefined();
    expect(byId["reasoning"]).toMatchObject({ default: "none" });
  });
  it("reads settings only this step uses as its own, whatever their name", () => {
    // A renamed step keeps the settings named after its old ID.
    const renamed = task(true);
    renamed.step.id = "triage";
    const own = state("form", renamed);
    const share = own.addable.find((o) => o.spec.id === "share")!.spec;
    expect(share.default).toBe("summarize");
    expect(share.choices![0]).toEqual({
      value: "summarize",
      label: "This step only",
    });
    expect(own.fields.map((f) => f.spec.id)).toEqual(["model", "prompt"]);
    // Settings named "<step>-2" because "<step>" was taken are this step's too.
    const taken = task(false, {
      summarize: { ...settings, model: "qwen2.5:1.5b" },
      "summarize-2": settings,
    });
    taken.step["profile"] = "summarize-2";
    taken.workflow.spec.steps.push({
      ...createStep("llm", "triage"),
      profile: "summarize",
    });
    const second = state("form", taken);
    expect(second.fields.map((f) => f.spec.id)).toEqual(["model", "prompt"]);
    expect(
      second.addable.find((o) => o.spec.id === "share")!.spec.choices,
    ).toEqual([
      { value: "summarize-2", label: "This step only" },
      { value: "summarize", label: "summarize · qwen2.5:1.5b" },
    ]);
  });
  it("offers settings of its own when it shares another step's", () => {
    const sharing = task(false, { triage: settings });
    sharing.step["profile"] = "triage";
    sharing.workflow.spec.steps.push({
      ...createStep("llm", "triage"),
      profile: "triage",
    });
    const share = state("form", sharing).fields.find(
      (f) => f.spec.id === "share",
    )!.spec;
    expect(share.default).toBe("summarize");
    expect(share.choices![0]).toEqual({
      value: "summarize",
      label: "This step only",
    });
  });
  it("offers this step's own settings or another AI step's to share", () => {
    const shared = task(true, {
      triage: { ...settings, model: "qwen2.5:1.5b" },
    });
    const share = state("form", shared).addable.find(
      (o) => o.spec.id === "share",
    )!.spec;
    expect(share).toMatchObject({
      type: "select",
      default: "summarize",
      whenRemoved: "default",
    });
    expect(share.choices).toEqual([
      { value: "summarize", label: "This step only" },
      { value: "triage", label: "triage · qwen2.5:1.5b" },
    ]);
  });
});

describe("the AI task's settings and canvas", () => {
  it("keeps the timeout on Settings, with the step's settings", () => {
    const before = state("settings", task(false)).fields;
    expect(before.map((f) => [f.spec.label, f.readOnly])).toEqual([
      ["Timeout", "Choose a model first."],
    ]);
    const [timeout] = state("settings", task(true)).fields;
    expect(timeout.readOnly).toBeNull();
    expect(timeout.spec).toMatchObject({
      label: "Timeout",
      type: "duration",
      scope: "workflow",
      path: ["spec", "llmProfiles", "summarize", "timeoutSeconds"],
      default: 120,
      max: 600,
    });
  });
  it("names its model on the canvas, asks for one before, and replays AI turns", () => {
    const descriptor = ndvRegistry.kind("llm")!;
    const fresh = task(false);
    expect(descriptor.summary(fresh.step, context(fresh.workflow))).toBe(
      "Choose a model",
    );
    const chosen = task(true);
    expect(descriptor.summary(chosen.step, context(chosen.workflow))).toBe(
      "qwen3:4b",
    );
    expect(descriptor.script).toBe("ai-turns");
    expect(descriptor.pinnable).toBe(true);
  });
});

describe("a structured result", () => {
  it("starts with no fields while the result is still text", () => {
    expect(designerSchema(TEXT_RESULT, TEXT_RESULT)).toBeUndefined();
    expect(designerSchema(undefined, TEXT_RESULT)).toBeUndefined();
    const fields = {
      type: "object",
      properties: { summary: { type: "string" } },
    };
    expect(designerSchema(fields, TEXT_RESULT)).toEqual(fields);
    // Other schema fields keep what they hold, defaults included.
    expect(designerSchema(undefined, { type: "object" })).toEqual({
      type: "object",
    });
    expect(designerSchema({ type: "string" }, { type: "object" })).toEqual({
      type: "string",
    });
  });
});

describe("an AI task's saved turn", () => {
  it("is one final answer with where it came from", () => {
    const { workflow, step } = task(true);
    expect(
      aiTaskTurn(workflow, step, "Two sentences.", { source: "simulated" }),
    ).toEqual({
      entry: { turns: [{ final: "Two sentences." }], source: "simulated" },
    });
    const run = {
      source: "test-call" as const,
      runId: "55555555-5555-4555-8555-555555555555",
      runEnvironmentId: "66666666-6666-4666-8666-666666666666",
    };
    expect(aiTaskTurn(workflow, step, "Done.", run)).toEqual({
      entry: { turns: [{ final: "Done." }], ...run },
    });
  });
  it("refuses an answer of another type than the result fields", () => {
    const { workflow, step } = task(true);
    expect(
      aiTaskTurn(workflow, step, { summary: "x" }, { source: "simulated" }),
    ).toEqual({ problem: "The sample doesn't match the result fields." });
    const profiles = workflow.spec["llmProfiles"] as Record<string, object>;
    profiles["summarize"] = {
      ...settings,
      outputSchema: {
        type: "object",
        properties: { summary: { type: "string" } },
      },
    };
    expect(
      aiTaskTurn(workflow, step, "Two sentences.", { source: "manual" }),
    ).toEqual({ problem: "The sample doesn't match the result fields." });
    expect(
      aiTaskTurn(workflow, step, { summary: "x" }, { source: "manual" }),
    ).toEqual({
      entry: { turns: [{ final: { summary: "x" } }], source: "manual" },
    });
  });
  it("refuses a turn for an AI task without settings", () => {
    const { workflow, step } = task(false);
    expect(aiTaskTurn(workflow, step, "x", { source: "simulated" })).toEqual({
      problem: "Choose a model first.",
    });
  });
  it("replays as the simulator does: the answer, no usage, the step's model", () => {
    const { workflow, step } = task(true);
    expect(
      aiTaskReplay(workflow, step, { turns: [{ final: "Two sentences." }] }),
    ).toEqual({
      result: "Two sentences.",
      usage: { requests: 0, inputTokens: 0, outputTokens: 0 },
      provider: "openai-chat",
      model: "qwen3:4b",
    });
    // Tool calls are an agent's: an AI task has nothing to replay.
    expect(
      aiTaskReplay(workflow, step, {
        turns: [{ toolCalls: [{ name: "x", arguments: {} }] }],
      }),
    ).toBeUndefined();
    expect(aiTaskReplay(workflow, step, { turns: [] })).toBeUndefined();
  });
});

describe("an AI task's usage line", () => {
  it("counts the requests and the tokens in and out", () => {
    const usage = (
      requests: number,
      inputTokens: number,
      outputTokens: number,
    ) => ({
      result: "x",
      usage: { requests, inputTokens, outputTokens },
    });
    expect(usageLine(usage(1, 812, 64))).toBe(
      "1 request · 812 in · 64 out tokens",
    );
    // A replay called no model: it says so instead of counting zeros.
    expect(usageLine(usage(0, 0, 0))).toBe(
      "No model call · replayed the saved answer",
    );
    expect(usageLine(usage(2, 12345, 1))).toBe(
      "2 requests · 12,345 in · 1 out token",
    );
  });
  it("is absent for an output without usage counts", () => {
    expect(usageLine({ result: "x" })).toBeNull();
    expect(usageLine({ usage: { requests: "1" } })).toBeNull();
    expect(usageLine("x")).toBeNull();
  });
});

const BOUND = {
  connectionId: "11111111-1111-4111-8111-111111111111",
  revisionId: "22222222-2222-4222-8222-222222222222",
  name: "ollama-local",
  tested: "ok" as const,
};
/** A step details context for the AI task, with the given binding and formula answers. */
function real(
  { workflow, step }: { workflow: Workflow; step: Step },
  bound: typeof BOUND | null = BOUND,
  evaluate: NdvContext["evaluate"] = (expression) =>
    "literal" in expression
      ? { ok: true, value: expression["literal"] as never }
      : { ok: false },
) {
  const ctx = {
    step,
    workflow,
    evaluate,
    connections: { devBinding: () => bound },
  } as unknown as NdvContext;
  return ndvRegistry.kind("llm")!.real!.request(ctx, context(workflow));
}
function prompted(chosen = true) {
  const made = task(chosen);
  made.step["prompt"] = { literal: "Summarize the ticket." };
  made.step["context"] = { literal: { ticket: "T-1" } };
  return made;
}

describe("an AI task run for real", () => {
  it("asks for a model before anything else", () => {
    expect(real(prompted(false))).toEqual({ blocked: "Choose a model first." });
  });
  it("asks for the AI connection to be bound", () => {
    expect(real(prompted(), null)).toEqual({
      blocked: "Bind this step's AI connection first.",
    });
  });
  it("sends the prompt, the context and the bound revision with the step's settings", () => {
    rememberConnections([
      {
        connection_id: BOUND.connectionId,
        revision_id: BOUND.revisionId,
        name: "ollama-local",
        connector: "weave-agentic-provider@1.0.0",
        label: "Ollama · ollama-local",
        enabled: true,
        provider: "openai-chat",
        endpoint_origin: "http://ollama:11434",
      },
    ]);
    expect(real(prompted())).toEqual({
      body: {
        kind: "llm",
        step_id: "summarize",
        uses: "weave-agentic-generate@1.0.0",
        profile: "summarize",
        input: { prompt: "Summarize the ticket.", context: { ticket: "T-1" } },
        connections: { ai: BOUND.revisionId },
        timeout_seconds: 120,
        // A local model call needs no question: the step says it accepts the call.
        acknowledged_side_effect: "non_idempotent",
      },
    });
  });
  it("says it needs earlier steps' data when the prompt can't be worked out", () => {
    const made = prompted();
    made.step["prompt"] = { template: "Summarize {{ steps.fetch.output }}" };
    expect(real(made)).toEqual({
      blocked:
        "summarize needs data from earlier steps. Execute previous steps first.",
    });
  });
  it("asks before a connection that isn't local, which may bill", () => {
    const cloud = {
      connectionId: "77777777-7777-4777-8777-777777777777",
      revisionId: "88888888-8888-4888-8888-888888888888",
      name: "openai-prod",
      tested: "ok" as const,
    };
    rememberConnections([
      {
        connection_id: cloud.connectionId,
        revision_id: cloud.revisionId,
        name: cloud.name,
        connector: "weave-agentic-provider@1.0.0",
        label: "OpenAI · openai-prod",
        enabled: true,
        provider: "openai-chat",
        endpoint_origin: "https://api.openai.com",
      },
    ]);
    const asked = real(prompted(), cloud);
    expect(asked).toMatchObject({
      body: { connections: { ai: cloud.revisionId } },
      // The provider, then the connection in parentheses.
      confirm:
        "This calls qwen3:4b on OpenAI (openai-prod) and may incur cost.",
    });
    expect("body" in asked && asked.body.acknowledged_side_effect).toBeFalsy();
    // A connection Studio hasn't listed is asked about too, by its name.
    const unknown = {
      ...cloud,
      revisionId: "99999999-9999-4999-8999-999999999999",
    };
    expect(real(prompted(), unknown)).toMatchObject({
      confirm: "This calls qwen3:4b on openai-prod and may incur cost.",
    });
  });
});

describe("which step tests run without asking", () => {
  /** An AI task's step test whose kind accepts the AI action's effect. */
  const localBody = () => ({
    body: {
      ...stepTestBody(task(true).workflow, task(true).step, BOUND.revisionId, {
        prompt: "p",
        context: null,
      }),
      acknowledged_side_effect: "non_idempotent" as const,
    },
  });
  it("an AI task on a local connection, for the AI action's own effect", () => {
    const local = localBody();
    expect(withoutAsking(local, "non_idempotent")).toBe("non_idempotent");
    // The action's contract isn't loaded yet: the AI action's effect is known.
    expect(withoutAsking(local, null)).toBe("non_idempotent");
  });
  it("never another action, another effect, or a step with a question", () => {
    const local = localBody();
    const action = {
      body: { ...local.body, kind: "action", uses: "acme.crm-cancel@1.0.0" },
    };
    expect(withoutAsking(action, "non_idempotent")).toBeNull();
    const renamed = { body: { ...local.body, uses: "acme.generate@1.0.0" } };
    expect(withoutAsking(renamed, "non_idempotent")).toBeNull();
    expect(withoutAsking(local, "idempotency_key")).toBeNull();
    const cloud = {
      ...local,
      confirm: "This calls x on y and may incur cost.",
    };
    expect(withoutAsking(cloud, "non_idempotent")).toBeNull();
    const unacknowledged = {
      body: { ...local.body, acknowledged_side_effect: null },
    };
    expect(withoutAsking(unacknowledged, "non_idempotent")).toBeNull();
  });
  it("after the question, an AI task acknowledges the AI action's effect", () => {
    const local = localBody();
    expect(effectToAcknowledge(local.body, null)).toBe("non_idempotent");
    expect(effectToAcknowledge(local.body, "idempotency_key")).toBe(
      "idempotency_key",
    );
    const action = { ...local.body, kind: "action", uses: "acme.x@1.0.0" };
    expect(effectToAcknowledge(action, null)).toBeNull();
  });
});

describe("an AI task's step test body", () => {
  it("runs for the step's timeout, at most five minutes", () => {
    const { workflow, step } = task(true);
    const profiles = workflow.spec["llmProfiles"] as Record<string, object>;
    profiles["summarize"] = { ...settings, timeoutSeconds: 600 };
    expect(
      stepTestBody(workflow, step, BOUND.revisionId, {
        prompt: "p",
        context: null,
      }).timeout_seconds,
    ).toBe(300);
    profiles["summarize"] = { ...settings, timeoutSeconds: 45 };
    expect(
      stepTestBody(workflow, step, BOUND.revisionId, {
        prompt: "p",
        context: null,
      }).timeout_seconds,
    ).toBe(45);
  });
  it("names the step's own slot", () => {
    const { workflow, step } = task(true);
    step["connection"] = "ai-openai-prod";
    expect(
      stepTestBody(workflow, step, BOUND.revisionId, {
        prompt: "p",
        context: null,
      }).connections,
    ).toEqual({ "ai-openai-prod": BOUND.revisionId });
  });
});

const RUN = "55555555-5555-4555-8555-555555555555";
const ENVIRONMENT = "66666666-6666-4666-8666-666666666666";
/**
 * Step details over one AI task (inside a loop over tickets when `loop`), with
 * test data in memory that the host accepts (or refuses as secret).
 */
function details(
  results: LastResults | null = null,
  secret = false,
  loop = false,
) {
  const model = new StructuredCanvasAdapter();
  const { workflow, step } = prompted();
  if (loop)
    workflow.spec.steps = [
      {
        id: "tickets",
        kind: "forEach",
        items: { ref: "/input/tickets" },
        concurrency: 1,
        maxItems: 1000,
        collect: "all",
        body: { steps: [step], output: { ref: "/item" } },
      },
    ];
  model.replace(workflow);
  model.clearHistory();
  const values = new Map<string, string>();
  const backend: StoreBackend = {
    keys: async () => [...values.keys()],
    get: async (key) => values.get(key) ?? null,
    set: async (key, value) => (values.set(key, value), true),
    remove: async (key) => values.delete(key),
  };
  const checked: string[][] = [];
  const api = {
    request: async <T>(path: string, _method?: string, body?: unknown) => {
      if (path !== "/studio/local/testdata/check") return { features: [] } as T;
      const data = (body as { testData: Record<string, unknown> }).testData;
      const paths = Object.keys((data["aiTurns"] as object) ?? {}).map(
        (key) => `/aiTurns/${key}`,
      );
      checked.push(paths);
      return {
        entries: paths.map((path) =>
          secret
            ? { path, ok: false, code: "WV-SCHEMA-SECRET_VALUE" }
            : { path, ok: true },
        ),
      } as T;
    },
  };
  const testData = new TestDataService({
    api,
    backend,
    now: () => new Date("2026-11-19T10:00:00Z"),
    inputs: () => ({
      source: "{}",
      definitions: [],
      schemaOf: () => ({}) as never,
    }),
  });
  const notices: string[] = [];
  /** Each toast's action (Undo), by the toast's text. */
  const actions = new Map<string, { label: string; run(): void }>();
  const host = {
    model,
    perform: (edit: () => void) => edit(),
    notify: (text: string, action?: { label: string; run(): void }) => {
      notices.push(text);
      if (action) actions.set(text, action);
    },
    undo: () => model.undo(),
    undoIfUnchanged: async () => "undone" as const,
    catalogContracts: new Map(),
    decisionContracts: new Map(),
    actionVersions: [],
    label: (kind: string) => kind,
    editingLocked: false,
    profile: null,
    api: api as never,
    diagnostics: null,
    diagnosticsDefinition: null,
    cacheDecisionContract: () => undefined,
    can: () => true,
    testData,
    openTestData: () => false,
    execution: {
      results: () => results,
      running: () => null,
      last: () => null,
      sampleUsed: () => null,
    } as never,
  } as unknown as ControllerHost;
  const controller = new StepDetailsController(host, () => 1000);
  return { controller, testData, checked, notices, actions, model };
}

describe("an AI task's test data in step details", () => {
  it("Generate sample saves a sample answer as the turn Execute step replays", async () => {
    const { controller, testData, checked } = details();
    expect(await controller.generateOutput("summarize")).toBeNull();
    // A text answer reads as one, named after the step.
    expect(testData.data().aiTurns?.["summarize"]).toEqual({
      turns: [{ final: "Sample answer from summarize." }],
      source: "simulated",
    });
    expect(testData.data().pins?.["summarize"]).toBeUndefined();
    expect(checked.at(-1)).toEqual(["/aiTurns/summarize"]);
    // The Output pane shows what Execute step would return.
    expect(controller.stepData("summarize")).toMatchObject({
      value: {
        result: "Sample answer from summarize.",
        usage: { requests: 0, inputTokens: 0, outputTokens: 0 },
        model: "qwen3:4b",
      },
      source: "simulated",
    });
    // The answer shown is the one kept as test data.
    expect(controller.keptAnswer("summarize")).toBe(true);
  });
  it("a text result's sample is its first example when the result fields give one", async () => {
    const { controller, testData, model } = details();
    const workflow = structuredClone(model.definition);
    (workflow.spec["llmProfiles"] as Record<string, Record<string, unknown>>)[
      "summarize"
    ]["outputSchema"] = {
      type: "string",
      maxLength: 20000,
      examples: ["The customer can't sign in."],
    };
    model.replace(workflow);
    expect(await controller.generateOutput("summarize")).toBeNull();
    expect(testData.data().aiTurns?.["summarize"]?.turns).toEqual([
      { final: "The customer can't sign in." },
    ]);
  });
  it("removes the kept answer on a second click, with Undo", async () => {
    const { controller, testData, notices, actions } = details();
    await controller.generateOutput("summarize");
    expect(await controller.forgetAnswer("summarize")).toBe(true);
    expect(testData.data().aiTurns).toBeUndefined();
    expect(controller.keptAnswer("summarize")).toBe(false);
    const removed = "Removed the kept answer for summarize.";
    expect(notices).toContain(removed);
    expect(actions.get(removed)?.label).toBe("Undo");
    actions.get(removed)!.run();
    expect(testData.data().aiTurns?.["summarize"]?.turns).toEqual([
      { final: "Sample answer from summarize." },
    ]);
  });
  it("reads and keeps the AI task's turn as its test answer", async () => {
    const { controller, testData } = details();
    const sample = controller.ndvContext("summarize").sample;
    expect(sample.script()).toBeNull();
    expect(
      await sample.setScript({ turns: [{ final: "Kept." }], source: "manual" }),
    ).toEqual({ ok: true });
    expect(testData.data().aiTurns?.["summarize"]).toEqual({
      turns: [{ final: "Kept." }],
      source: "manual",
    });
    expect(sample.script()).toEqual({
      turns: [{ final: "Kept." }],
      source: "manual",
    });
  });
  it("keeps a real answer as test data with its run, never as a pin", async () => {
    const { controller, testData, notices } = details(realAnswer());
    expect(controller.keptAnswer("summarize")).toBe(false);
    expect(await controller.pinReal("summarize")).toBeNull();
    // The real answer shown is now the kept one (the same run).
    expect(controller.keptAnswer("summarize")).toBe(true);
    expect(testData.data().aiTurns?.["summarize"]).toEqual({
      turns: [{ final: "Two sentences." }],
      source: "test-call",
      runId: RUN,
      runEnvironmentId: ENVIRONMENT,
    });
    expect(testData.data().pins?.["summarize"]).toBeUndefined();
    expect(notices).toContain(
      "Kept this answer as test data for summarize. Execute step replays it.",
    );
  });
  it("keeps no answer the host finds secret, and says why without the value", async () => {
    const secret =
      "This value is marked secret in the schema. Test data can't keep secrets; leave the field out.";
    const real = details(realAnswer(), true);
    expect(await real.controller.pinReal("summarize")).toBe(secret);
    expect(real.testData.data().aiTurns).toBeUndefined();
    expect(real.notices.join(" ")).not.toContain("Two sentences.");
    const sample = details(null, true);
    expect(await sample.controller.generateOutput("summarize")).toBe(secret);
    expect(sample.testData.data().aiTurns).toBeUndefined();
  });
});

describe("an AI task's kept answer inside a loop", () => {
  it("Generate sample keeps the answer for the item picked only", async () => {
    const { controller, testData } = details(null, false, true);
    controller.iterations.pick("tickets", 1);
    expect(await controller.generateOutput("summarize")).toBeNull();
    expect(Object.keys(testData.data().aiTurns ?? {})).toEqual([
      "summarize[1]",
    ]);
    expect(controller.stepData("summarize[1]")?.value).toMatchObject({
      result: "Sample answer from summarize.",
    });
    expect(controller.stepData("summarize[0]")).toBeUndefined();
  });
  it("keeps a real answer for the item picked, and says which item", async () => {
    const { controller, testData, notices } = details(
      realAnswer(),
      false,
      true,
    );
    controller.iterations.pick("tickets", 1);
    expect(await controller.pinReal("summarize")).toBeNull();
    expect(Object.keys(testData.data().aiTurns ?? {})).toEqual([
      "summarize[1]",
    ]);
    expect(notices).toContain(
      "Kept this answer as test data for summarize, item 2. Execute step replays it.",
    );
  });
  it("removes the kept answer of the item picked only, with Undo", async () => {
    const { controller, testData, notices, actions } = details(
      null,
      false,
      true,
    );
    controller.iterations.pick("tickets", 0);
    await controller.generateOutput("summarize");
    controller.iterations.pick("tickets", 1);
    await controller.generateOutput("summarize");
    expect(Object.keys(testData.data().aiTurns ?? {}).sort()).toEqual([
      "summarize[0]",
      "summarize[1]",
    ]);
    expect(await controller.forgetAnswer("summarize")).toBe(true);
    expect(Object.keys(testData.data().aiTurns ?? {})).toEqual([
      "summarize[0]",
    ]);
    const removed = "Removed the kept answer for summarize, item 2.";
    expect(notices).toContain(removed);
    expect(actions.get(removed)?.label).toBe("Undo");
  });
  it("removes an answer kept for every item from any item picked", async () => {
    const { controller, testData, notices } = details(null, false, true);
    expect(
      await controller.setScript("summarize", {
        turns: [{ final: "Sample answer from summarize." }],
        source: "manual",
      }),
    ).toEqual({ ok: true });
    controller.iterations.pick("tickets", 1);
    // Item 2 replays the step's answer, so it shows as kept.
    expect(controller.keptAnswer("summarize")).toBe(true);
    expect(await controller.forgetAnswer("summarize")).toBe(true);
    expect(testData.data().aiTurns).toBeUndefined();
    // Today's words name the step, whose answer it was.
    expect(notices).toContain("Removed the kept answer for summarize.");
  });
});

describe("replacing a kept answer", () => {
  it("offers Undo, as replacing a pin does", async () => {
    const { controller, testData, notices, actions } = details(realAnswer());
    await controller.generateOutput("summarize");
    expect(await controller.pinReal("summarize")).toBeNull();
    const replaced = "Replaced the kept answer for summarize.";
    expect(notices).toContain(replaced);
    expect(actions.get(replaced)?.label).toBe("Undo");
    actions.get(replaced)!.run();
    expect(testData.data().aiTurns?.["summarize"]?.turns).toEqual([
      { final: "Sample answer from summarize." },
    ]);
  });
});

describe("keeping the connection chosen with a model", () => {
  const connection = {
    connection_id: BOUND.connectionId,
    revision_id: BOUND.revisionId,
    name: "ollama-local",
    connector: "weave-agentic-provider@1.0.0",
    label: "Ollama · ollama-local",
    enabled: true,
  };
  const port = (setBinding: () => Promise<boolean>) => ({ setBinding });
  it("keeps it as the slot's binding", async () => {
    const calls: unknown[][] = [];
    const kept = {
      setBinding: async (...args: unknown[]) => (calls.push(args), true),
    };
    expect(
      await bindChosen(kept, "support-inbox", "ai", connection),
    ).toBeNull();
    expect(calls).toEqual([
      [
        "support-inbox",
        "ai",
        {
          connectionId: BOUND.connectionId,
          revisionId: BOUND.revisionId,
          name: "ollama-local",
        },
      ],
    ]);
  });
  it("says so when this computer can't keep it, or keeping it fails", async () => {
    const words =
      "ollama-local is the connection for slot ai for now, but Studio couldn't keep the choice on this computer.";
    expect(
      await bindChosen(
        port(async () => false),
        "support-inbox",
        "ai",
        connection,
      ),
    ).toBe(words);
    expect(
      await bindChosen(
        port(async () => {
          throw new Error("storage is full");
        }),
        "support-inbox",
        "ai",
        connection,
      ),
    ).toBe(words);
  });
});

/** The AI task's last result: a real answer from a step test in development. */
function realAnswer(): LastResults {
  return {
    schemaVersion: 1,
    at: "2026-11-19T10:00:00Z",
    status: "completed",
    steps: {
      summarize: {
        status: "completed",
        execution: "fnv1a64:0000000000000000",
        output: {
          result: "Two sentences.",
          usage: { requests: 1, inputTokens: 812, outputTokens: 64 },
          provider: "openai-chat",
          model: "qwen3:4b",
        },
        real: {
          runId: RUN,
          environmentId: ENVIRONMENT,
          environmentName: "development",
          status: "succeeded",
        },
      },
    },
  };
}

describe("whether a saved turn holds the answer shown", () => {
  const shown = { result: "Two sentences.", usage: { requests: 1 } };
  it("holds it for the same run, or the same final answer", () => {
    const fromRun = {
      turns: [{ final: "Other words." }],
      source: "test-call",
      runId: RUN,
      runEnvironmentId: ENVIRONMENT,
    };
    expect(holdsAnswer(fromRun, shown, RUN)).toBe(true);
    expect(
      holdsAnswer({ turns: [{ final: "Two sentences." }] }, shown, null),
    ).toBe(true);
    // Structured answers compare by value, whatever the key order.
    expect(
      holdsAnswer(
        { turns: [{ final: { b: 2, a: 1 } }] },
        { result: { a: 1, b: 2 } },
        null,
      ),
    ).toBe(true);
  });
  it("doesn't for another answer, another run, or no turn at all", () => {
    expect(holdsAnswer({ turns: [{ final: "Other." }] }, shown, RUN)).toBe(
      false,
    );
    expect(holdsAnswer(null, shown, RUN)).toBe(false);
    expect(
      holdsAnswer({ turns: [{ final: "Two sentences." }] }, null, null),
    ).toBe(false);
  });
});
