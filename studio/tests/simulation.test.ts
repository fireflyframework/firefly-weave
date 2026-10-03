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
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { execFileSync, spawn, type ChildProcess } from "node:child_process";
import { createInterface } from "node:readline";
import { join, resolve } from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import {
  ValueEditor,
  advanceOptions,
  advanceSeconds,
  breakpointsCommand,
  createBody,
  decisionCommand,
  sampleValue,
  signalCommand,
  simulationPlan,
  stepResults,
  type DebugCommandBody,
  type DebugCreateBody,
  type DebugViewData,
} from "../src/app/designer/simulation/simulation-plan";
import {
  debugMessages,
  durationText,
  eventText,
  simulationMessage,
  statusLabel,
} from "../src/app/designer/simulation/simulation-copy";
import { workflowTemplates } from "../src/app/templates/catalog";

const root = resolve(import.meta.dirname, "../..");
const python = resolve(
  root,
  process.platform === "win32"
    ? ".venv/Scripts/python.exe"
    : ".venv/bin/python",
);
const available = existsSync(python);
const env = { ...process.env, PYTHONPATH: resolve(root, "src") };

describe("sample values shaped by schemas", () => {
  it("fills required fields and leaves text for the person to enter", () => {
    expect(
      sampleValue({
        type: "object",
        required: ["status", "body", "count", "kind", "tags"],
        properties: {
          status: { const: 200 },
          body: {
            type: "object",
            required: ["name", "ok"],
            properties: {
              name: { type: "string" },
              ok: { type: "boolean" },
              extra: { type: "string" },
            },
          },
          count: { type: "integer", exclusiveMinimum: 2 },
          kind: { enum: ["a", "b"] },
          tags: { type: "array" },
          optional: { type: "number" },
        },
      }),
    ).toEqual({
      status: 200,
      body: { name: "", ok: false },
      count: 3,
      kind: "a",
      tags: [],
    });
    expect(sampleValue({ type: "number", minimum: 5 })).toBe(5);
    expect(sampleValue({ type: ["null", "string"] })).toBe("");
    expect(sampleValue({ type: "null" })).toBeNull();
    expect(sampleValue({})).toBeUndefined();
    expect(sampleValue(true)).toBeUndefined();
  });
});

describe("value editors", () => {
  const schema = {
    type: "object",
    required: ["name", "ok"],
    properties: { name: { type: "string" }, ok: { type: "boolean" } },
  };

  it("uses the form for object schemas and reports missing fields", () => {
    const editor = new ValueEditor(schema, sampleValue(schema));
    expect(editor.formCapable).toBe(true);
    expect(editor.jsonMode).toBe(false);
    expect(editor.problem()).toBe("Fill in name.");
    editor.formChange({ name: "Ada", ok: false });
    expect(editor.problem()).toBe("");
    editor.formValid = false;
    expect(editor.problem()).toBe("Correct the fields marked with an error.");
  });

  it("round-trips through JSON and refuses to leave invalid JSON", () => {
    const editor = new ValueEditor(schema, { name: "Ada", ok: true });
    editor.toggle();
    expect(editor.jsonMode).toBe(true);
    expect(JSON.parse(editor.json)).toEqual({ name: "Ada", ok: true });
    editor.editJson("{");
    expect(editor.problem()).toBe("Enter valid JSON.");
    editor.toggle();
    expect(editor.jsonMode).toBe(true);
    editor.editJson('{"name":"Grace","ok":false}');
    editor.toggle();
    expect(editor.jsonMode).toBe(false);
    expect(editor.initial).toEqual({ name: "Grace", ok: false });
    expect(editor.value).toEqual({ name: "Grace", ok: false });
  });

  it("sends the {} it shows for a schema without a sample value", () => {
    for (const schema of [{}, { description: "Any JSON" }]) {
      const editor = new ValueEditor(schema, sampleValue(schema));
      expect(editor.jsonMode).toBe(true);
      expect(JSON.parse(editor.json)).toEqual(editor.value);
      expect(editor.problem()).toBe("");
      // Not skipped, so the result must reach the simulator.
      expect(
        createBody({}, {}, { call: editor.value }, new Date(0)).mocks,
      ).toEqual({ "node:call": {} });
    }
  });

  it("edits schemas without properties as JSON only", () => {
    const editor = new ValueEditor({ type: "string" }, "");
    expect(editor.formCapable).toBe(false);
    expect(editor.jsonMode).toBe(true);
    expect(editor.json).toBe('""');
    editor.toggle();
    expect(editor.jsonMode).toBe(true);
  });
});

describe("simulation copy", () => {
  it("explains every debugger code the platform can return", () => {
    const sources = [
      join(root, "src/firefly_weave/api/debug.py"),
      join(root, "src/firefly_weave/cli/debug.py"),
      ...readdirSync(join(root, "src/firefly_weave/operations/debug"))
        .filter((f) => f.endsWith(".py"))
        .map((f) => join(root, "src/firefly_weave/operations/debug", f)),
    ];
    const codes = new Set(
      sources.flatMap((file) =>
        [...readFileSync(file, "utf8").matchAll(/WV-DEBUG-[A-Z_-]+/g)].map(
          (m) => m[0],
        ),
      ),
    );
    expect(codes.size).toBeGreaterThan(10);
    for (const code of codes) expect(debugMessages, code).toHaveProperty(code);
    for (const message of Object.values(debugMessages)) {
      expect(message).not.toMatch(/WV-|node:|mock/i);
      expect(message.endsWith(".")).toBe(true);
    }
    expect(simulationMessage("WV-OTHER", "Kept as is.")).toBe("Kept as is.");
  });

  it("describes statuses, events and durations in plain words", () => {
    expect(statusLabel("timed_out")).toBe("Timed out");
    expect(statusLabel("something_new")).toBe("something new");
    expect(eventText({ type: "started" })).toBe("Run started");
    expect(
      eventText({
        type: "human_completed",
        data: { node_id: "review", output: { decision: "approve" } },
      }),
    ).toBe("Human task review decided: approve");
    expect(eventText({ type: "timed_out", data: { node_id: "@run" } })).toBe(
      "The run reached its time limit",
    );
    expect(durationText(60)).toBe("1 min");
    expect(durationText(86400)).toBe("1 day");
    expect(durationText(90061)).toBe("1 day 1 h");
    expect(durationText(5400)).toBe("1 h 30 min");
    expect(durationText(0)).toBe("0 s");
  });

  it("accepts only whole, non-negative custom advances", () => {
    expect(advanceSeconds("2", 3600)).toBe(7200);
    expect(advanceSeconds(" 0 ", 60)).toBe(0);
    expect(advanceSeconds("1.5", 60)).toBeNull();
    expect(advanceSeconds("-1", 1)).toBeNull();
    expect(advanceSeconds("", 1)).toBeNull();
    expect(advanceSeconds("9".repeat(20), 86400)).toBeNull();
  });
});

// Compile the templates with the Python compiler, then drive the real
// simulator with exactly the bodies the panel emits.
const artifacts: Record<string, Record<string, unknown>> = available
  ? JSON.parse(
      execFileSync(
        python,
        [
          "-c",
          `
import json, pathlib, sys
import yaml
from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.contracts.definitions import load_definition

templates = json.loads(sys.stdin.read())
sample = yaml.safe_load(pathlib.Path("examples/definitions/check-customer.action.yaml").read_text())
stub = {"apiVersion": "weave/v1alpha1", "kind": "Action",
        "metadata": {"name": "your-action", "version": "1.0.0"},
        "spec": {"implementation": {"kind": "worker", "taskType": "your-action", "taskVersion": "1.0.0"},
                 "sideEffect": "read_only", "timeoutSeconds": 60,
                 "inputSchema": {"type": "object"},
                 "outputSchema": {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}}}}
def task(name, definition):
    spec = definition["spec"]
    return {"taskType": name, "taskVersion": "1.0.0", "inputSchema": spec["inputSchema"],
            "outputSchema": spec["outputSchema"], "sideEffect": "read_only", "timeoutSeconds": 60}
catalog = CatalogSnapshot.from_definitions(
    [load_definition(sample), load_definition(stub)],
    tasks=[task("onboarding.check-customer", sample), task("your-action", stub)])
out = {}
for t in templates:
    if "weave-http@2.0.0" in t["yaml"]:
        continue
    result = compile_source(t["yaml"], format="yaml", catalog=catalog)
    assert result.ok, (t["id"], [d.code for d in result.diagnostics])
    out[t["id"]] = json.loads(result.artifact.to_bytes())
print(json.dumps(out))
`,
        ],
        {
          cwd: root,
          env,
          encoding: "utf8",
          timeout: 30000,
          input: JSON.stringify(
            workflowTemplates.map(({ id, yaml }) => ({ id, yaml })),
          ),
        },
      ),
    )
  : {};

type Reply = { view?: DebugViewData; error?: string };
class Simulator {
  private process: ChildProcess;
  private replies: ((reply: Reply) => void)[] = [];
  constructor() {
    this.process = spawn(
      python,
      [
        "-u",
        "-c",
        `
import json, sys
from firefly_weave.compiler.api import import_artifact
from firefly_weave.operations.debug.models import DebugCommand, DebugCreate, DebugError
from firefly_weave.operations.debug.simulator import Simulator
sessions = {}
for line in sys.stdin:
    request = json.loads(line)
    try:
        if request["op"] == "create":
            # The API parses JSON bodies exactly like this (api/debug.py).
            body = DebugCreate.model_validate_json(json.dumps(request["body"]))
            simulator = Simulator(import_artifact(body.artifact), mocks=body.mocks, input=body.input, now=body.now)
            sessions[request["id"]] = simulator
            view = simulator.inspect()
        else:
            command = DebugCommand.model_validate_json(json.dumps(request["body"]))
            view = sessions[request["id"]].command(command)
        reply = {"view": view.model_dump(mode="json")}
    except DebugError as error:
        reply = {"error": error.code}
    except ValueError:
        reply = {"error": "WV-VALIDATION"}
    except Exception as error:
        reply = {"error": "unexpected " + type(error).__name__}
    print(json.dumps(reply), flush=True)
`,
      ],
      { cwd: root, env, stdio: ["pipe", "pipe", "inherit"] },
    );
    createInterface({ input: this.process.stdout! }).on("line", (line) =>
      this.replies.shift()?.(JSON.parse(line) as Reply),
    );
  }
  private call(message: object): Promise<Reply> {
    return new Promise((resolveReply) => {
      this.replies.push(resolveReply);
      this.process.stdin!.write(JSON.stringify(message) + "\n");
    });
  }
  create(id: string, body: DebugCreateBody) {
    return this.call({ op: "create", id, body });
  }
  command(id: string, body: DebugCommandBody) {
    return this.call({ op: "command", id, body });
  }
  stop() {
    this.process.kill();
  }
}

describe.skipIf(!available)("panel bodies against the real simulator", () => {
  let simulator: Simulator;
  const now = new Date("2026-01-01T00:00:00Z");
  beforeAll(() => {
    simulator = new Simulator();
  });
  afterAll(() => simulator.stop());

  it("reads nodes, signal names, schemas and action mocks from the artifact", () => {
    const plan = simulationPlan(artifacts["customer-onboarding"]);
    expect(plan.steps.map((s) => [s.id, s.kind])).toEqual([
      ["check", "action"],
      ["approval", "signal"],
      ["decision", "transform"],
    ]);
    expect(plan.inputSchema).toMatchObject({ required: ["customerId"] });
    expect(plan.actions).toEqual([
      expect.objectContaining({
        nodeId: "check",
        key: "node:check",
        reference: "onboarding.check-customer@1.0.0",
        outputSchema: expect.objectContaining({ required: ["eligible"] }),
      }),
    ]);
    expect(plan.signals).toEqual([
      expect.objectContaining({
        nodeId: "approval",
        name: "customer-approved",
        timeoutSeconds: 86400,
        payloadSchema: expect.objectContaining({ required: ["approved"] }),
      }),
    ]);
    expect(simulationPlan(null).steps).toEqual([]);
    expect(simulationPlan({ executable: { graph: "x" } }).actions).toEqual([]);
  });

  it("runs customer onboarding to completion with form-shaped values", async () => {
    const artifact = artifacts["customer-onboarding"];
    const plan = simulationPlan(artifact);
    const result = sampleValue(plan.actions[0].outputSchema) as Record<
      string,
      unknown
    >;
    expect(result).toEqual({ eligible: false });
    const body = createBody(
      artifact,
      { customerId: "c-1" },
      { check: { ...result, eligible: true } },
      now,
    );
    expect(Object.keys(body.mocks)).toEqual(["node:check"]);
    expect((await simulator.create("onboarding", body)).view?.status).toBe(
      "queued",
    );
    const waiting = (
      await simulator.command("onboarding", { kind: "continue" })
    ).view!;
    expect(waiting.status).toBe("waiting");
    expect(waiting.active_nodes).toEqual(["approval"]);
    expect(advanceOptions(waiting, plan)).toEqual([
      {
        nodeId: "approval",
        label: "Let approval time out (1 day)",
        seconds: 86400,
      },
    ]);
    const signal = signalCommand(plan.signals[0], {
      ...(sampleValue(plan.signals[0].payloadSchema) as object),
      approved: true,
    });
    expect(signal).toEqual({
      kind: "signal",
      name: "customer-approved",
      payload: { approved: true },
    });
    expect((await simulator.command("onboarding", signal)).error).toBe(
      undefined,
    );
    const done = (await simulator.command("onboarding", { kind: "continue" }))
      .view!;
    expect(done.status).toBe("succeeded");
    expect(done.variables["output"]).toEqual({ accepted: true });
    expect(stepResults(done, plan).map((r) => r.stepId)).toEqual([
      "check",
      "approval",
      "decision",
    ]);
  });

  it("addresses a signal by name; the node ID is rejected", async () => {
    const artifact = artifacts["customer-onboarding"];
    await simulator.create(
      "by-node",
      createBody(
        artifact,
        { customerId: "c" },
        { check: { eligible: true } },
        now,
      ),
    );
    const reply = await simulator.command("by-node", {
      kind: "signal",
      name: "approval",
      payload: { approved: true },
    });
    expect(reply.error).toBe("WV-DEBUG-SIGNAL");
  });

  it("rejects mocks keyed by bare step IDs, which the panel never sends", async () => {
    const reply = await simulator.create("bare", {
      ...createBody(
        artifacts["customer-onboarding"],
        { customerId: "c" },
        {},
        now,
      ),
      mocks: { check: { eligible: true } },
    });
    expect(reply.error).toBe("WV-DEBUG-MOCK-KEY");
  });

  it("stops at an action left without a result, with plain copy", async () => {
    await simulator.create(
      "skipped",
      createBody(
        artifacts["customer-onboarding"],
        { customerId: "c" },
        { check: undefined },
        now,
      ),
    );
    const view = (await simulator.command("skipped", { kind: "continue" }))
      .view!;
    expect(view.status).toBe("failed");
    expect(view.diagnostics.map((d) => d.code)).toEqual([
      "WV-DEBUG-MISSING_MOCK",
    ]);
    expect(simulationMessage(view.diagnostics[0].code, "")).toMatch(
      /no result/,
    );
  });

  it("decides a human task by node ID and follows the chosen branch", async () => {
    const artifact = artifacts["approval-branch"];
    const plan = simulationPlan(artifact);
    expect(plan.humans).toEqual([
      expect.objectContaining({
        nodeId: "review",
        decisions: ["approve", "reject"],
      }),
    ]);
    await simulator.create(
      "approval",
      createBody(artifact, { requestId: "r-1", amount: 10 }, {}, now),
    );
    const waiting = (await simulator.command("approval", { kind: "continue" }))
      .view!;
    expect(waiting.active_nodes).toEqual(["review"]);
    const decision = decisionCommand(plan.humans[0], "reject", { note: "No" });
    expect(decision).toEqual({
      kind: "human_decision",
      name: "review",
      payload: { decision: "reject", data: { note: "No" } },
    });
    expect((await simulator.command("approval", decision)).error).toBe(
      undefined,
    );
    const done = (await simulator.command("approval", { kind: "continue" }))
      .view!;
    expect(done.status).toBe("succeeded");
    expect(done.variables["output"]).toEqual({ approved: false, note: "No" });
    expect(stepResults(done, plan).map((r) => r.stepId)).toEqual([
      "review",
      "route",
      "rejected",
    ]);
  });

  it("advances to a signal deadline and times the run out", async () => {
    const artifact = artifacts["signal-timeout"];
    const plan = simulationPlan(artifact);
    await simulator.create(
      "timeout",
      createBody(artifact, { orderId: "o-1" }, {}, now),
    );
    const waiting = (await simulator.command("timeout", { kind: "continue" }))
      .view!;
    const [option] = advanceOptions(waiting, plan);
    expect(option).toMatchObject({ nodeId: "confirmation", seconds: 86400 });
    const reached = (
      await simulator.command("timeout", {
        kind: "advance_time",
        seconds: option.seconds,
      })
    ).view!;
    // The deadline is reached; only Continue applies it, so no "0 s" preset.
    expect(reached.active_nodes).toEqual(["confirmation"]);
    expect(advanceOptions(reached, plan)).toEqual([]);
    const done = (await simulator.command("timeout", { kind: "continue" }))
      .view!;
    expect(done.status).toBe("timed_out");
  });

  it("pauses at breakpoints inside parallel branches and resumes", async () => {
    const artifact = artifacts["parallel-merge"];
    const plan = simulationPlan(artifact);
    expect(plan.actions.map((a) => a.key).sort()).toEqual([
      "node:get-orders",
      "node:get-profile",
    ]);
    const results = Object.fromEntries(
      plan.actions.map((a) => [a.nodeId, { id: a.nodeId }]),
    );
    await simulator.create(
      "parallel",
      createBody(artifact, { customerId: "c-1" }, results, now),
    );
    const breakpoints = breakpointsCommand(plan, ["merge", "unknown"]);
    expect(breakpoints).toEqual({ kind: "breakpoints", node_ids: ["merge"] });
    expect((await simulator.command("parallel", breakpoints)).error).toBe(
      undefined,
    );
    const paused = (await simulator.command("parallel", { kind: "continue" }))
      .view!;
    expect(paused.status).toBe("paused");
    expect(paused.current_nodes).toContain("merge");
    const done = (await simulator.command("parallel", { kind: "continue" }))
      .view!;
    expect(done.status).toBe("succeeded");
    expect(done.variables["output"]).toEqual({
      customerId: "c-1",
      profile: { id: "get-profile" },
      orders: { id: "get-orders" },
    });
    expect(stepResults(done, plan).map((r) => r.stepId)).toEqual([
      "lookups",
      "get-orders",
      "get-profile",
      "merge",
    ]);
  });
});
