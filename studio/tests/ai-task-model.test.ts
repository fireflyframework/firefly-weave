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
import { execFileSync } from "node:child_process";
import { describe, expect, it } from "vitest";
import {
  AI_ACTION,
  AI_CONNECTOR,
  DEFAULT_MAX_TOKENS,
  TEXT_RESULT,
  aiTaskSetup,
  aiTurnsProblem,
  chooseModel,
  currentModelValue,
  deferredToSetup,
  isTextResult,
  modelOptions,
  modelValue,
  profileNameOf,
  profileOf,
  sharingNote,
  stepsSharing,
  type ConnectionModels,
} from "../src/app/editor/ndv/kinds/ai-task-model";
import { applyChange } from "../src/app/editor/ndv/params/value-io";
import type { BindableConnection } from "../src/app/integrations/ai-setup-api";
import {
  createStep,
  freshWorkflow,
  type Step,
  type Workflow,
} from "../src/app/model";
import { python, pythonAvailable } from "./python-path";

const ollama: BindableConnection = {
  connection_id: "c-ollama",
  revision_id: "r-ollama",
  name: "ollama-local",
  connector: AI_CONNECTOR,
  label: "Ollama · ollama-local",
  enabled: true,
  provider: "openai-chat",
  endpoint_origin: "http://ollama:11434",
};
const cloud: BindableConnection = {
  connection_id: "c-openai",
  revision_id: "r-openai",
  name: "openai",
  connector: AI_CONNECTOR,
  label: "OpenAI · openai",
  enabled: true,
  provider: "openai-responses",
  endpoint_origin: "https://api.openai.com",
};
const qwen = {
  name: "qwen3:4b",
  approved: true,
  available: true,
  tools: "yes" as const,
  context_tokens: 8192,
};
const small = {
  name: "qwen2.5:1.5b",
  approved: true,
  available: true,
  tools: "yes" as const,
};
const gemma = {
  name: "gemma3:270m",
  approved: false,
  available: true,
  tools: "no" as const,
};
const groups: ConnectionModels[] = [
  { connection: ollama, models: [qwen, small, gemma] },
  {
    connection: cloud,
    models: [
      { name: "gpt-5-mini", approved: true, available: true, tools: "yes" },
    ],
  },
];

function withTask(id = "summarize"): { workflow: Workflow; step: Step } {
  const workflow = freshWorkflow();
  const step = createStep("llm", id);
  workflow.spec.steps.push(step);
  return { workflow, step };
}
/** The workflow after the changes, as the editor applies them. */
function applied(
  workflow: Workflow,
  step: Step,
  changes: ReturnType<typeof chooseModel>,
) {
  let subject = { workflow, step, action: null, roots: [] } as any;
  for (const change of changes) subject = applyChange(subject, change);
  const next = structuredClone(subject.workflow) as Workflow;
  const index = next.spec.steps.findIndex((s: Step) => s.id === step.id);
  next.spec.steps[index] = subject.step;
  return next;
}
const choice = (connection: BindableConnection, model: string) => ({
  connectionId: connection.connection_id,
  revisionId: connection.revision_id,
  connection: connection.name,
  provider: connection.provider!,
  model,
});

describe("a new AI task", () => {
  it("needs a model until its settings exist", () => {
    const { workflow, step } = withTask();
    expect(step).toEqual({
      id: "summarize",
      kind: "llm",
      uses: AI_ACTION,
      profile: "default",
      connection: "ai",
      prompt: { literal: "" },
      context: { literal: {} },
    });
    expect(profileNameOf(step)).toBe("default");
    expect(profileOf(workflow, step)).toBeNull();
    expect(aiTaskSetup(workflow, step)).toBe("Choose a model");
  });
  it("shows the compiler's messages as setup until a model is chosen", () => {
    const { workflow } = withTask();
    expect(deferredToSetup(workflow, "summarize")).toBe(true);
    expect(deferredToSetup(workflow, "other")).toBe(false);
  });
});

describe("choosing a model", () => {
  it("writes the settings, the ai slot and the action in one change", () => {
    const { workflow, step } = withTask();
    const next = applied(
      workflow,
      step,
      chooseModel(workflow, step, choice(ollama, "qwen3:4b")),
    );
    expect(next.spec["llmProfiles"]).toEqual({
      summarize: {
        provider: "openai-chat",
        model: "qwen3:4b",
        options: { max_tokens: DEFAULT_MAX_TOKENS },
        outputSchema: TEXT_RESULT,
      },
    });
    expect(next.spec["connections"]).toEqual({
      ai: { connector: AI_CONNECTOR, required: true },
    });
    expect(next.spec.steps[0]).toMatchObject({
      profile: "summarize",
      connection: "ai",
      uses: AI_ACTION,
    });
    expect(aiTaskSetup(next, next.spec.steps[0])).toBe("");
    expect(deferredToSetup(next, "summarize", ["profile"], "LLM_PROFILE")).toBe(
      false,
    );
  });
  it("keeps the options and result when only the model changes", () => {
    const { workflow, step } = withTask();
    step["profile"] = "summarize";
    workflow.spec["llmProfiles"] = {
      summarize: {
        provider: "openai-chat",
        model: "qwen3:4b",
        options: { max_tokens: 300, temperature: 0 },
        outputSchema: { type: "object", properties: { a: { type: "string" } } },
        timeoutSeconds: 60,
      },
    };
    workflow.spec["connections"] = {
      ai: { connector: AI_CONNECTOR, required: true },
    };
    const next = applied(
      workflow,
      step,
      chooseModel(workflow, step, choice(cloud, "gpt-5-mini")),
    );
    expect(next.spec["llmProfiles"]).toEqual({
      summarize: {
        provider: "openai-responses",
        model: "gpt-5-mini",
        options: { max_tokens: 300, temperature: 0 },
        outputSchema: { type: "object", properties: { a: { type: "string" } } },
        timeoutSeconds: 60,
      },
    });
  });
  it("gives a second AI connection its own slot once the first slot is bound to another", () => {
    const { workflow, step } = withTask();
    workflow.spec["connections"] = {
      ai: { connector: AI_CONNECTOR, required: true },
    };
    const bound = (slot: string) =>
      slot === "ai" ? ollama.connection_id : null;
    const next = applied(
      workflow,
      step,
      chooseModel(workflow, step, choice(cloud, "gpt-5-mini"), bound),
    );
    expect(next.spec["connections"]).toEqual({
      ai: { connector: AI_CONNECTOR, required: true },
      "ai-openai": { connector: AI_CONNECTOR, required: true },
    });
    expect(next.spec.steps[0]["connection"]).toBe("ai-openai");
    const same = applied(
      workflow,
      step,
      chooseModel(workflow, step, choice(ollama, "qwen3:4b"), bound),
    );
    expect(same.spec.steps[0]["connection"]).toBe("ai");
  });
  it("keeps an existing ai slot as it is", () => {
    const { workflow, step } = withTask();
    workflow.spec["connections"] = {
      ai: { connector: AI_CONNECTOR, required: false },
    };
    const changes = chooseModel(workflow, step, choice(ollama, "qwen3:4b"));
    expect(changes.some((c) => c.path[1] === "connections")).toBe(false);
    const next = applied(workflow, step, changes);
    expect(next.spec["connections"]).toEqual({
      ai: { connector: AI_CONNECTOR, required: false },
    });
    expect(next.spec.steps[0]["connection"]).toBe("ai");
  });
  it("never points an AI task at a slot of another connector", () => {
    const { workflow, step } = withTask();
    const mail = { connector: "weave-email@1.0.0", required: true };
    workflow.spec["connections"] = { ai: mail };
    const next = applied(
      workflow,
      step,
      chooseModel(workflow, step, choice(ollama, "qwen3:4b")),
    );
    expect(next.spec["connections"]).toEqual({
      ai: mail,
      "ai-ollama-local": { connector: AI_CONNECTOR, required: true },
    });
    expect(next.spec.steps[0]["connection"]).toBe("ai-ollama-local");
    workflow.spec["connections"] = { ai: mail, "ai-ollama-local": mail };
    const taken = applied(
      workflow,
      step,
      chooseModel(workflow, step, choice(ollama, "qwen3:4b")),
    );
    expect(taken.spec["connections"]).toMatchObject({
      ai: mail,
      "ai-ollama-local": mail,
      "ai-ollama-local-2": { connector: AI_CONNECTOR, required: true },
    });
    expect(taken.spec.steps[0]["connection"]).toBe("ai-ollama-local-2");
  });
  it("gives a new AI task its own settings beside settings named default", () => {
    const { workflow, step } = withTask();
    const classic = {
      provider: "anthropic",
      model: "claude-x",
      options: { max_tokens: 4000 },
      outputSchema: { type: "object", properties: {} },
    };
    workflow.spec["llmProfiles"] = { default: classic };
    workflow.spec.steps.unshift({ ...createStep("llm", "triage") });
    expect(step["profile"]).toBe("default");
    const next = applied(
      workflow,
      step,
      chooseModel(workflow, step, choice(ollama, "qwen3:4b")),
    );
    expect(next.spec["llmProfiles"]).toEqual({
      default: classic,
      summarize: {
        provider: "openai-chat",
        model: "qwen3:4b",
        options: { max_tokens: DEFAULT_MAX_TOKENS },
        outputSchema: TEXT_RESULT,
      },
    });
    expect(next.spec.steps.map((s: Step) => [s.id, s["profile"]])).toEqual([
      ["triage", "default"],
      ["summarize", "summarize"],
    ]);
  });
  it("says when the settings are shared, and what a new model changes", () => {
    const { workflow, step } = withTask();
    expect(sharingNote(workflow, step)).toBe("");
    workflow.spec["llmProfiles"] = {
      default: { provider: "anthropic", model: "claude-x" },
      triage: { provider: "anthropic", model: "claude-x" },
    };
    workflow.spec.steps.unshift({ ...createStep("llm", "triage") });
    expect(sharingNote(workflow, step)).toBe(
      "Uses the default settings, like 1 other AI step. Choosing a model gives this step its own.",
    );
    step["profile"] = "triage";
    workflow.spec.steps[0]["profile"] = "triage";
    workflow.spec.steps.push({
      ...createStep("llm", "reply"),
      profile: "triage",
    });
    expect(sharingNote(workflow, step)).toBe(
      "Shared with 2 other AI steps. A new model changes theirs too.",
    );
  });
  it("changes every AI task that shares the settings", () => {
    const { workflow, step } = withTask();
    const other = { ...createStep("llm", "review"), profile: "summarize" };
    step["profile"] = "summarize";
    workflow.spec.steps.push(other);
    workflow.spec["llmProfiles"] = {
      summarize: {
        provider: "openai-chat",
        model: "qwen3:4b",
        options: { max_tokens: 1024 },
        outputSchema: TEXT_RESULT,
      },
    };
    expect(stepsSharing(workflow, "summarize")).toEqual([
      "summarize",
      "review",
    ]);
    const next = applied(
      workflow,
      other,
      chooseModel(workflow, other, choice(ollama, "qwen2.5:1.5b")),
    );
    expect(Object.keys(next.spec["llmProfiles"] as object)).toEqual([
      "summarize",
    ]);
    expect((next.spec["llmProfiles"] as any).summarize.model).toBe(
      "qwen2.5:1.5b",
    );
  });
  it("names new settings after the step, or another free name", () => {
    const { workflow, step } = withTask();
    workflow.spec["llmProfiles"] = {
      summarize: {
        provider: "openai-chat",
        model: "m",
        options: { max_tokens: 1 },
        outputSchema: TEXT_RESULT,
      },
    };
    const next = applied(
      workflow,
      step,
      chooseModel(workflow, step, choice(ollama, "qwen3:4b")),
    );
    expect(Object.keys(next.spec["llmProfiles"] as object)).toEqual([
      "summarize",
      "summarize-2",
    ]);
    expect(next.spec.steps[0]["profile"]).toBe("summarize-2");
  });
  it("refuses a 33rd set of AI settings", () => {
    const { workflow, step } = withTask("one-more");
    workflow.spec["llmProfiles"] = Object.fromEntries(
      Array.from({ length: 32 }, (_, i) => [
        `task-${i}`,
        { provider: "openai-chat", model: "m" },
      ]),
    );
    expect(() =>
      chooseModel(workflow, step, choice(ollama, "qwen3:4b")),
    ).toThrow(
      "A workflow holds at most 32 sets of AI settings. Share the settings of another AI step instead.",
    );
  });
});

describe("the model list", () => {
  it("groups models by AI connection and shows what each can do", () => {
    const offered = [
      {
        ...groups[0],
        models: [
          ...groups[0].models!,
          { ...small, name: "llama3.2:3b", available: false },
        ],
      },
      groups[1],
    ];
    expect(modelOptions(offered)).toEqual([
      {
        value: modelValue("r-ollama", "qwen3:4b"),
        label: "qwen3:4b",
        description: "Calls tools · 8,192 tokens of context · Local",
        group: "Ollama · ollama-local",
      },
      {
        value: modelValue("r-ollama", "qwen2.5:1.5b"),
        label: "qwen2.5:1.5b",
        description: "Calls tools · Local",
        group: "Ollama · ollama-local",
      },
      {
        value: modelValue("r-ollama", "gemma3:270m"),
        label: "gemma3:270m",
        description:
          "Not approved for this connection. Approve it in Settings › AI models.",
        group: "Ollama · ollama-local",
        disabled: true,
      },
      {
        value: modelValue("r-openai", "gpt-5-mini"),
        label: "gpt-5-mini",
        description: "Calls tools",
        group: "OpenAI · openai",
      },
    ]);
  });
  it("says when a connection's models can't be listed", () => {
    expect(
      modelOptions([
        {
          connection: ollama,
          models: null,
          error: "The AI gateway is not running.",
        },
      ]),
    ).toEqual([
      {
        value: modelValue("r-ollama", ""),
        label: "Models unavailable",
        description: "The AI gateway is not running.",
        group: "Ollama · ollama-local",
        disabled: true,
      },
    ]);
  });
  it("finds the chosen model on the bound connection, else on the one connection that serves it", () => {
    const { workflow, step } = withTask();
    step["profile"] = "summarize";
    expect(currentModelValue(workflow, step, groups)).toBe("");
    workflow.spec["llmProfiles"] = {
      summarize: {
        provider: "openai-chat",
        model: "qwen3:4b",
        options: { max_tokens: 1024 },
        outputSchema: TEXT_RESULT,
      },
    };
    expect(currentModelValue(workflow, step, groups)).toBe(
      modelValue("r-ollama", "qwen3:4b"),
    );
    const second = {
      ...ollama,
      connection_id: "c-two",
      revision_id: "r-two",
      name: "ollama-two",
    };
    const both = [...groups, { connection: second, models: [qwen] }];
    // Two connections serve it: the one just chosen, else the first listed.
    expect(currentModelValue(workflow, step, both)).toBe(
      modelValue("r-ollama", "qwen3:4b"),
    );
    expect(currentModelValue(workflow, step, both, () => null, "r-two")).toBe(
      modelValue("r-two", "qwen3:4b"),
    );
    expect(currentModelValue(workflow, step, both, () => "c-two")).toBe(
      modelValue("r-two", "qwen3:4b"),
    );
    // A connection where the model is not approved or not available doesn't serve it.
    const unapproved = [
      { connection: ollama, models: [{ ...qwen, approved: false }] },
      { connection: second, models: [qwen] },
    ];
    expect(currentModelValue(workflow, step, unapproved)).toBe(
      modelValue("r-two", "qwen3:4b"),
    );
    const gone = [
      { connection: ollama, models: [{ ...qwen, available: false }] },
    ];
    expect(currentModelValue(workflow, step, gone)).toBe("");
  });
});

describe("results", () => {
  it("treats a plain string schema as text, like the worker", () => {
    expect(isTextResult(TEXT_RESULT)).toBe(true);
    expect(isTextResult({ type: "string" })).toBe(true);
    expect(
      isTextResult({
        type: "string",
        title: "Summary",
        description: "Two sentences.",
        minLength: 1,
        maxLength: 400,
      }),
    ).toBe(true);
    expect(isTextResult({ type: "string", enum: ["a"] })).toBe(false);
    expect(isTextResult({ type: "object", properties: {} })).toBe(false);
  });
});

const VALID_TURNS: unknown[] = [
  { turns: [{ final: "Two sentences." }] },
  {
    turns: [
      { toolCalls: [{ name: "lookup_order", arguments: { id: "A-17" } }] },
      { final: { ok: true }, text: "Done." },
    ],
  },
  { turns: [], memory: [{ role: "user", content: "Hi" }], autoApprove: true },
  {
    turns: [{ final: 1 }],
    source: "run",
    runId: "3f2a6c1e-0d5b-4a63-9c1f-2b7e8d9a0b11",
    runEnvironmentId: "9b1c2d3e-4f50-4a61-8b72-c3d4e5f60718",
  },
  { turns: [], configHash: "fnv1a64:0123456789abcdef" },
];
/** Entries that break exactly one rule, with the place it is reported at. */
const SINGLE_RULES: [unknown, string][] = [
  [{ turns: [], source: "run" }, ""],
  [{ turns: [], source: "test-call" }, ""],
  [
    {
      turns: [],
      runId: "3f2a6c1e-0d5b-4a63-9c1f-2b7e8d9a0b11",
      runEnvironmentId: "9b1c2d3e-4f50-4a61-8b72-c3d4e5f60718",
    },
    "",
  ],
  [
    {
      turns: [],
      source: "manual",
      runId: "3f2a6c1e-0d5b-4a63-9c1f-2b7e8d9a0b11",
    },
    "",
  ],
  [{ turns: [], memory: [{ role: "user" }] }, "/memory/0"],
];
const INVALID_TURNS: unknown[] = [
  {},
  [],
  { turns: [{ text: "Thinking." }] },
  { turns: [{ result: "Shipped." }] },
  { turns: [{ final: 1, toolCalls: [{ name: "x", arguments: {} }] }] },
  { turns: [{ toolCalls: [] }] },
  { turns: [{ toolCalls: [{ name: "x" }] }] },
  { turns: [{ toolCalls: Array(17).fill({ name: "x", arguments: {} }) }] },
  { turns: Array(34).fill({ final: 1 }) },
  { turns: [{ final: 1, text: 2 }] },
  { turns: [], memory: [{ role: "system", content: "x" }] },
  { turns: [], reviews: {} },
  { turns: [], source: "schema" },
  { turns: [], source: "run", runId: "3f2a6c1e-0d5b-4a63-9c1f-2b7e8d9a0b11" },
  { turns: [], configHash: "fnv1a64:0123456789abcdef\n" },
  { turns: [], runId: "3f2a6c1e0d5b4a639c1f2b7e8d9a0b11" },
  ...SINGLE_RULES.map(([entry]) => entry),
  ...[
    "12345678-1234-1234-1234-1234-5678-9012",
    "{3f2a6c1e-0d5b-4a63-9c1f-2b7e8d9a0b11}",
    "3f2a6c1e-0d5b-4a63-9c1f-2b7e8d9a0b11}",
    "urn:uuid:3f2a6c1e-0d5b-4a63-9c1f-2b7e8d9a0b11",
    "\u0663f2a6c1e-0d5b-4a63-9c1f-2b7e8d9a0b11",
  ].map((runId) => ({
    turns: [],
    source: "run",
    runId,
    runEnvironmentId: "9b1c2d3e-4f50-4a61-8b72-c3d4e5f60718",
  })),
];

describe("recorded AI turns", () => {
  it("accepts what the schema accepts and names the first problem's place otherwise", () => {
    for (const entry of VALID_TURNS) expect(aiTurnsProblem(entry)).toBeNull();
    for (const entry of INVALID_TURNS)
      expect(typeof aiTurnsProblem(entry)).toBe("string");
    expect(aiTurnsProblem({ turns: [{ final: 1 }, { result: 2 }] })).toBe(
      "/turns/1",
    );
    for (const [entry, place] of SINGLE_RULES)
      expect(aiTurnsProblem(entry)).toBe(place);
  });
});

describe.skipIf(!pythonAvailable())(
  "recorded AI turns match the platform",
  () => {
    it("accepts and refuses the same entries as the published schema", () => {
      const cases = [...VALID_TURNS, ...INVALID_TURNS];
      const output = execFileSync(
        python,
        [
          "-c",
          "import json, sys; from firefly_weave.contracts.studio_documents import ai_turns_problems; " +
            "print(json.dumps([not ai_turns_problems(entry) for entry in json.load(sys.stdin)]))",
        ],
        { cwd: "..", encoding: "utf8", input: JSON.stringify(cases) },
      );
      expect(JSON.parse(output)).toEqual(
        cases.map((entry) => aiTurnsProblem(entry) === null),
      );
    });
  },
);
