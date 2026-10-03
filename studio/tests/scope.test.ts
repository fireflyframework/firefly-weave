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
import { existsSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { delimiter, resolve } from "node:path";
import { describe, expect, it } from "vitest";
import {
  WORKFLOW,
  compatibility,
  escapeSegment,
  referenceScope,
  typeLabel,
  visibleRefs,
  visibleSteps,
} from "../src/app/forms/core/scope";

type Json = Record<string, unknown>;
const lit = (value: unknown) => ({ literal: value });
const ref = (pointer: string) => ({ ref: pointer });
const transform = (id: string, value: unknown = lit({})) => ({
  id,
  kind: "transform",
  value,
});
const action = (id: string, uses = "lookup@1.0.0") => ({
  id,
  kind: "action",
  uses,
  with: lit({}),
});
const branch = (steps: Json[], output: unknown = lit({})) => ({
  steps,
  output,
});
const sw = (id: string, cases: Json[][], fallback: Json[]) => ({
  id,
  kind: "switch",
  cases: cases.map((steps) => ({ when: lit(true), ...branch(steps) })),
  default: branch(fallback),
});
const par = (id: string, branches: Record<string, Json[]>) => ({
  id,
  kind: "parallel",
  concurrency: 2,
  branches: Object.fromEntries(
    Object.entries(branches).map(([name, steps]) => [name, branch(steps)]),
  ),
});
const wait = (id: string) => ({ id, kind: "wait", durationSeconds: 60 });
const fail = (id: string) => ({
  id,
  kind: "fail",
  code: "stopped",
  message: "Stopped on purpose",
});
const signal = (id: string, payloadSchema: Json = { type: "object" }) => ({
  id,
  kind: "signal",
  name: `${id}-received`,
  timeoutSeconds: 3600,
  payloadSchema,
});
const human = (id: string, formSchema: Json = { type: "object" }) => ({
  id,
  kind: "humanTask",
  title: lit("Review request"),
  context: lit({}),
  assignment: "reviewers",
  formSchema,
  decisions: ["approve", "reject"],
});
const workflow = (steps: Json[], inputSchema: Json = { type: "object" }) => ({
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name: "scope-probe", version: "1.0.0" },
  spec: {
    inputSchema,
    outputSchema: {},
    steps,
    output: lit({}),
  },
});

const fixtures: Record<string, ReturnType<typeof workflow>> = {
  sequence: workflow([transform("a"), action("b"), wait("c"), transform("d")]),
  switch: workflow([
    transform("s1"),
    sw(
      "sw",
      [
        [transform("t1"), transform("t2")],
        [transform("t3"), sw("inner", [[transform("i1")]], [transform("i2")])],
      ],
      [transform("t4")],
    ),
    transform("t5"),
  ]),
  nestedParallel: workflow([
    transform("before"),
    par("p", {
      a: [
        transform("x1"),
        par("q", { m: [transform("y1")], n: [transform("y2")] }),
        transform("x3"),
      ],
      b: [transform("x2")],
    }),
    transform("z"),
  ]),
  humanTask: workflow([
    transform("prepare"),
    human("review", {
      type: "object",
      properties: { comment: { type: "string" } },
    }),
    transform("after"),
    par("fanout", {
      left: [human("left-review"), transform("left-after")],
      right: [transform("right-only")],
    }),
  ]),
  signal: workflow([
    signal("approval", {
      type: "object",
      required: ["approved"],
      properties: { approved: { type: "boolean" } },
    }),
    sw("route", [[signal("nested-signal"), transform("n1")]], []),
    transform("done"),
  ]),
  failure: workflow([
    transform("one"),
    sw("guard", [[transform("c1"), fail("stop"), transform("dead")]], []),
    transform("two"),
    par("half", { ok: [transform("h1")], ko: [fail("h2")] }),
    transform("three"),
    fail("end"),
    transform("unreachable"),
  ]),
  // A parallel inside a case, an action inside a branch, and a decision
  // whose every path fails.
  caseParallel: workflow([
    transform("start"),
    sw(
      "route",
      [
        [
          transform("c0"),
          par("fan", {
            left: [action("l1"), human("l2")],
            right: [sw("inner", [[fail("never")]], [transform("r1")])],
          }),
          transform("c0-after"),
        ],
        [fail("c1-stop")],
      ],
      [signal("d0"), transform("d1")],
    ),
    sw("doomed", [[fail("x1")]], [fail("x2")]),
    transform("after-doomed"),
  ]),
};

const ids = (steps: { id: string }[] | null) =>
  steps === null ? null : steps.map((s) => s.id);

describe("visible steps mirror the compiler's lexical dominance", () => {
  it("a sequence sees earlier siblings only", () => {
    const w = fixtures.sequence;
    expect(ids(visibleSteps(w, "a", "/value"))).toEqual([]);
    expect(ids(visibleSteps(w, "d", "/value"))).toEqual(["a", "b", "c"]);
    expect(ids(visibleSteps(w, "b", "with"))).toEqual(["a"]);
    expect(ids(visibleSteps(w, WORKFLOW, "/spec/output"))).toEqual([
      "a",
      "b",
      "c",
      "d",
    ]);
  });
  it("a branch sees the group's context plus earlier branch steps, never the group or siblings", () => {
    const w = fixtures.switch;
    expect(ids(visibleSteps(w, "t2", "/value"))).toEqual(["s1", "t1"]);
    expect(ids(visibleSteps(w, "t3", "/value"))).toEqual(["s1"]);
    expect(ids(visibleSteps(w, "t4", "/value"))).toEqual(["s1"]);
    expect(ids(visibleSteps(w, "i1", "/value"))).toEqual(["s1", "t3"]);
    expect(ids(visibleSteps(w, "sw", "/cases/1/when"))).toEqual(["s1"]);
    expect(ids(visibleSteps(w, "sw", "/cases/0/output"))).toEqual([
      "s1",
      "t1",
      "t2",
    ]);
    expect(ids(visibleSteps(w, "sw", "/cases/1/output"))).toEqual([
      "s1",
      "t3",
      "inner",
    ]);
    expect(ids(visibleSteps(w, "sw", "/default/output"))).toEqual(["s1", "t4"]);
    expect(ids(visibleSteps(w, "t5", "/value"))).toEqual(["s1", "sw"]);
  });
  it("nested parallel branches stay isolated", () => {
    const w = fixtures.nestedParallel;
    expect(ids(visibleSteps(w, "y1", "/value"))).toEqual(["before", "x1"]);
    expect(ids(visibleSteps(w, "y2", "/value"))).toEqual(["before", "x1"]);
    expect(ids(visibleSteps(w, "x3", "/value"))).toEqual(["before", "x1", "q"]);
    expect(ids(visibleSteps(w, "x2", "/value"))).toEqual(["before"]);
    expect(ids(visibleSteps(w, "q", "/branches/m/output"))).toEqual([
      "before",
      "x1",
      "y1",
    ]);
    expect(ids(visibleSteps(w, "p", "/branches/a/output"))).toEqual([
      "before",
      "x1",
      "q",
      "x3",
    ]);
    expect(ids(visibleSteps(w, "z", "/value"))).toEqual(["before", "p"]);
  });
  it("fail ends reachability; a failing path hides its group", () => {
    const w = fixtures.failure;
    expect(ids(visibleSteps(w, "dead", "/value"))).toEqual(["one", "c1"]);
    expect(ids(visibleSteps(w, "two", "/value"))).toEqual(["one", "guard"]);
    expect(ids(visibleSteps(w, "three", "/value"))).toEqual([
      "one",
      "guard",
      "two",
    ]);
    // "half" never completes, so nothing after it becomes visible.
    expect(ids(visibleSteps(w, "unreachable", "/value"))).toEqual([
      "one",
      "guard",
      "two",
    ]);
    const deadOutput = referenceScope(w, "guard", "/cases/0/output");
    expect(deadOutput.evaluated).toBe(false);
    expect(referenceScope(w, "guard", "/default/output").evaluated).toBe(true);
    expect(referenceScope(w, WORKFLOW, "/spec/output").evaluated).toBe(false);
  });
  it("a parallel inside a case sees the case; a group that always fails ends reachability", () => {
    const w = fixtures.caseParallel;
    expect(ids(visibleSteps(w, "l2", "/title"))).toEqual(["start", "c0", "l1"]);
    expect(ids(visibleSteps(w, "r1", "/value"))).toEqual(["start", "c0"]);
    expect(ids(visibleSteps(w, "fan", "/branches/right/output"))).toEqual([
      "start",
      "c0",
      "inner",
    ]);
    expect(ids(visibleSteps(w, "c0-after", "/value"))).toEqual([
      "start",
      "c0",
      "fan",
    ]);
    expect(ids(visibleSteps(w, "route", "/cases/0/output"))).toEqual([
      "start",
      "c0",
      "fan",
      "c0-after",
    ]);
    expect(referenceScope(w, "route", "/cases/1/output").evaluated).toBe(false);
    expect(ids(visibleSteps(w, "after-doomed", "/value"))).toEqual([
      "start",
      "route",
    ]);
    expect(referenceScope(w, WORKFLOW, "/spec/output").evaluated).toBe(false);
  });
  it("reports unknown targets instead of guessing", () => {
    const w = fixtures.sequence;
    expect(visibleSteps(w, "missing", "/value")).toBeNull();
    expect(visibleSteps(w, WORKFLOW, "/spec/inputSchema")).toBeNull();
    expect(referenceScope(w, "missing", "/value").found).toBe(false);
    expect(visibleRefs(w, "missing", "/value")).toEqual([]);
    expect(ids(visibleSteps(w, "d", "value"))).toEqual(["a", "b", "c"]);
  });
});

describe("typed reference suggestions", () => {
  it("expands the workflow input and step outputs with type labels", () => {
    const w = workflow(
      [
        action("check", "onboarding.check-customer@1.0.0"),
        transform("decision"),
      ],
      {
        type: "object",
        required: ["customerId"],
        properties: {
          customerId: { type: "string", title: "Customer ID" },
          amount: { type: "number" },
        },
      },
    );
    const refs = visibleRefs(w, "decision", "/value", {
      actionOutput: (uses) =>
        uses === "onboarding.check-customer@1.0.0"
          ? {
              type: "object",
              required: ["eligible"],
              properties: { eligible: { type: "boolean" } },
            }
          : undefined,
    });
    expect(refs.map((r) => r.ref)).toEqual([
      "/input",
      "/input/customerId",
      "/input/amount",
      "/steps/check/output",
      "/steps/check/output/eligible",
    ]);
    const customer = refs.find((r) => r.ref === "/input/customerId")!;
    expect(customer).toMatchObject({
      source: "input",
      label: "Customer ID",
      breadcrumb: "Workflow input › Customer ID",
      types: ["string"],
      typeLabel: "Text",
      optional: false,
    });
    expect(refs.find((r) => r.ref === "/input/amount")!.optional).toBe(true);
    expect(
      refs.find((r) => r.ref === "/steps/check/output/eligible"),
    ).toMatchObject({
      source: "step",
      stepId: "check",
      stepKind: "action",
      breadcrumb: "check › eligible",
      typeLabel: "Yes or no",
    });
    const unknown = visibleRefs(w, "decision", "/value");
    expect(unknown.find((r) => r.ref === "/steps/check/output")).toMatchObject({
      types: [],
      typeLabel: "Any value",
    });
  });
  it("types human task decisions, signal payloads and transform shapes", () => {
    const refs = visibleRefs(fixtures.humanTask, "after", "/value");
    expect(
      refs.find((r) => r.ref === "/steps/review/output/decision"),
    ).toMatchObject({
      typeLabel: "Choice",
      types: ["string"],
      optional: false,
    });
    expect(
      refs.find((r) => r.ref === "/steps/review/output/data/comment"),
    ).toMatchObject({ typeLabel: "Text", optional: true });
    const signals = visibleRefs(fixtures.signal, "done", "/value");
    expect(
      signals.find((r) => r.ref === "/steps/approval/output/approved"),
    ).toMatchObject({ typeLabel: "Yes or no", optional: false });
    const shaped = workflow([
      transform("shape", {
        object: {
          total: lit(3),
          ok: { op: { name: "and", args: [lit(true), lit(false)] } },
          copy: ref("/input/name"),
        },
      }),
      transform("use"),
    ]);
    shaped.spec.inputSchema = {
      type: "object",
      required: ["name"],
      properties: { name: { type: "string" } },
    };
    const shapedRefs = visibleRefs(shaped, "use", "/value");
    expect(
      Object.fromEntries(
        shapedRefs
          .filter((r) => r.stepId === "shape")
          .map((r) => [r.ref, r.typeLabel]),
      ),
    ).toEqual({
      "/steps/shape/output": "Object",
      "/steps/shape/output/total": "Whole number",
      "/steps/shape/output/ok": "Yes or no",
      "/steps/shape/output/copy": "Text",
    });
    const waits = visibleRefs(fixtures.sequence, "d", "/value");
    expect(waits.find((r) => r.ref === "/steps/c/output")!.typeLabel).toBe(
      "Empty",
    );
  });
  it("parallel outputs expose one field per branch; a decision keeps only fields every path sets", () => {
    const w = workflow([
      par("both", { left: [], right: [] }),
      {
        ...sw("pick", [[]], []),
        cases: [
          {
            when: lit(true),
            steps: [],
            output: lit({ shared: "a", onlyCase: 1 }),
          },
        ],
        default: { steps: [], output: lit({ shared: "b" }) },
      },
      transform("use"),
    ]);
    const refs = visibleRefs(w, "use", "/value").map((r) => r.ref);
    expect(refs).toContain("/steps/both/output/left");
    expect(refs).toContain("/steps/both/output/right");
    expect(refs).toContain("/steps/pick/output/shared");
    expect(refs).not.toContain("/steps/pick/output/onlyCase");
  });
  it("bounds depth to 4 and entries to 200, always keeping every root", () => {
    const deep = (depth: number): Json =>
      depth === 0
        ? { type: "string" }
        : { type: "object", properties: { next: deep(depth - 1) } };
    const wide = Object.fromEntries(
      Array.from({ length: 300 }, (_, i) => [`f${i}`, { type: "string" }]),
    );
    const w = workflow(
      [
        signal("deep", deep(6) as Json),
        signal("wide", { type: "object", properties: wide }),
        transform("use"),
      ],
      { type: "object", properties: wide },
    );
    const scope = referenceScope(w, "use", "/value");
    expect(scope.entries).toHaveLength(200);
    expect(scope.truncated).toBe(true);
    const all = scope.entries.map((e) => e.ref);
    expect(all).toEqual(
      expect.arrayContaining([
        "/input",
        "/steps/deep/output",
        "/steps/wide/output",
      ]),
    );
    const deepOnly = referenceScope(
      workflow([signal("deep", deep(6) as Json), transform("use")]),
      "use",
      "/value",
    );
    expect(deepOnly.truncated).toBe(false);
    expect(Math.max(...deepOnly.entries.map((e) => e.path.length))).toBe(4);
    expect(deepOnly.entries.map((e) => e.ref)).toContain(
      "/steps/deep/output/next/next/next/next",
    );
  });
  it("escapes property names as RFC 6901 pointers and resolves local $defs", () => {
    const w = workflow([transform("use")], {
      type: "object",
      properties: {
        "a/b": { type: "string" },
        "~home": { $ref: "#/$defs/address" },
      },
      $defs: {
        address: {
          type: "object",
          properties: { city: { type: "string" } },
        },
      },
    });
    const refs = visibleRefs(w, "use", "/value").map((r) => r.ref);
    expect(refs).toEqual([
      "/input",
      "/input/a~1b",
      "/input/~0home",
      "/input/~0home/city",
    ]);
    expect(escapeSegment("x~/y")).toBe("x~0~1y");
  });
  it("labels types in plain words and judges compatibility lightly", () => {
    expect(typeLabel({ type: ["string", "null"] })).toBe("Text or empty");
    expect(typeLabel({ type: "array", items: { type: "integer" } })).toBe(
      "List of whole numbers",
    );
    expect(typeLabel({ type: "string", format: "date-time" })).toBe(
      "Date and time",
    );
    expect(typeLabel({ const: "x" })).toBe("Fixed value");
    expect(typeLabel({})).toBe("Any value");
    expect(compatibility({ type: "integer" }, { type: "number" })).toBe(
      "compatible",
    );
    expect(compatibility({ type: "string" }, { type: "number" })).toBe(
      "incompatible",
    );
    expect(compatibility({}, { type: "number" })).toBe("unknown");
    expect(
      compatibility({ type: ["string", "null"] }, { type: "string" }),
    ).toBe("unknown");
    expect(
      compatibility(
        { type: "string", enum: ["a"] },
        { type: "string", enum: ["a", "b"] },
      ),
    ).toBe("compatible");
    expect(
      compatibility(
        { type: "string", enum: ["c"] },
        { type: "string", enum: ["a", "b"] },
      ),
    ).toBe("incompatible");
  });
});

// Cross-language parity: every expression position of every fixture is probed
// with a reference to every step. The compiler is the authority on which of
// those references are available.
const root = resolve(import.meta.dirname, "../..");
const python = resolve(
  root,
  process.platform === "win32"
    ? ".venv/Scripts/python.exe"
    : ".venv/bin/python",
);
const COMPILER_PROBE = `
import json, sys
try:
    from firefly_weave.compiler.api import validate_authoring as check
    mode = "authoring"
except ImportError:
    from firefly_weave.compiler.api import validate_source as check
    mode = "source"
results = []
for document in json.load(sys.stdin):
    result = check(json.dumps(document), format="json")
    results.append({
        "validationOk": result.validation_ok,
        "diagnostics": [d.model_dump(by_alias=True, exclude_none=True) for d in result.diagnostics],
    })
print(json.dumps({"mode": mode, "results": results}))
`;
interface Position {
  fixture: string;
  stepId: string;
  fieldPath: string;
  pointer: string;
}
function positions(name: string, definition: Json): Position[] {
  const out: Position[] = [];
  const visit = (steps: Json[], base: string) =>
    steps.forEach((step, index) => {
      const at = `${base}/${index}`;
      const id = step["id"] as string;
      const add = (fieldPath: string) =>
        out.push({
          fixture: name,
          stepId: id,
          fieldPath,
          pointer: at + fieldPath,
        });
      const fields: Record<string, string[]> = {
        action: ["/with"],
        transform: ["/value"],
        humanTask: ["/title", "/context"],
      };
      (fields[step["kind"] as string] ?? []).forEach(add);
      if (step["kind"] === "switch") {
        (step["cases"] as Json[]).forEach((c, j) => {
          add(`/cases/${j}/when`);
          add(`/cases/${j}/output`);
          visit(c["steps"] as Json[], `${at}/cases/${j}/steps`);
        });
        add("/default/output");
        visit(
          (step["default"] as Json)["steps"] as Json[],
          `${at}/default/steps`,
        );
      }
      if (step["kind"] === "parallel")
        for (const [branchName, b] of Object.entries(
          step["branches"] as Record<string, Json>,
        )) {
          add(`/branches/${escapeSegment(branchName)}/output`);
          visit(
            b["steps"] as Json[],
            `${at}/branches/${escapeSegment(branchName)}/steps`,
          );
        }
    });
  visit((definition["spec"] as Json)["steps"] as Json[], "/spec/steps");
  out.push({
    fixture: name,
    stepId: WORKFLOW,
    fieldPath: "/spec/output",
    pointer: "/spec/output",
  });
  return out;
}
function allIds(definition: Json): string[] {
  const out: string[] = [];
  const visit = (steps: Json[]) =>
    steps.forEach((step) => {
      out.push(step["id"] as string);
      const nested = [
        ...((step["cases"] as Json[] | undefined) ?? []),
        ...(step["default"] ? [step["default"] as Json] : []),
        ...Object.values((step["branches"] as Record<string, Json>) ?? {}),
      ];
      nested.forEach((b) => visit(b["steps"] as Json[]));
    });
  visit((definition["spec"] as Json)["steps"] as Json[]);
  return out;
}
function setAt(document: Json, pointer: string, value: unknown) {
  const segments = pointer
    .split("/")
    .slice(1)
    .map((s) => s.replace(/~1/g, "/").replace(/~0/g, "~"));
  let node = document as Record<string, unknown>;
  for (const segment of segments.slice(0, -1))
    node = node[segment] as Record<string, unknown>;
  node[segments[segments.length - 1]] = value;
}
const probes = Object.entries(fixtures).flatMap(([name, definition]) => {
  const candidates = allIds(definition);
  return positions(name, definition).map((position) => {
    const document = structuredClone(definition) as Json;
    setAt(document, position.pointer, {
      array: candidates.map((id) => ref(`/steps/${id}/output`)),
    });
    return { position, candidates, document };
  });
});
const available = existsSync(python);
const compiled = available
  ? (JSON.parse(
      execFileSync(python, ["-c", COMPILER_PROBE], {
        cwd: root,
        encoding: "utf8",
        timeout: 180_000,
        input: JSON.stringify(probes.map((p) => p.document)),
        env: {
          ...process.env,
          PYTHONPATH: [resolve(root, "src"), process.env.PYTHONPATH]
            .filter(Boolean)
            .join(delimiter),
        },
      }),
    ) as {
      mode: "authoring" | "source";
      results: {
        validationOk: boolean;
        diagnostics: { code: string; path: string; severity: string }[];
      }[];
    })
  : null;

describe.skipIf(!available)("scope parity with the Python compiler", () => {
  it("covers at least five fixtures and every expression position", () => {
    expect(Object.keys(fixtures).length).toBeGreaterThanOrEqual(5);
    expect(probes.length).toBeGreaterThan(40);
    expect(compiled!.results).toHaveLength(probes.length);
  });
  it("every probe document is structurally valid authoring input", () => {
    probes.forEach((probe, i) => {
      const blocking = compiled!.results[i].diagnostics.filter((d) =>
        /^WV-(PARSE|SCHEMA|EXPR)-/.test(d.code),
      );
      expect(blocking, JSON.stringify(probe.position)).toEqual([]);
    });
  });
  it("suggests exactly the steps the compiler accepts at each position", (context) => {
    context.skip(
      compiled!.mode !== "authoring",
      "validate_authoring is not available yet; validate_source performs no dominance analysis, so availability cannot be compared",
    );
    let rejected = 0;
    probes.forEach((probe, i) => {
      const { position, candidates } = probe;
      const prefix = `${position.pointer}/array/`;
      const flagged = new Set(
        compiled!.results[i].diagnostics
          .filter(
            (d) =>
              d.code === "WV-COMP-UNAVAILABLE_REFERENCE" &&
              d.path.startsWith(prefix),
          )
          .map(
            (d) =>
              candidates[Number(d.path.slice(prefix.length).split("/")[0])],
          ),
      );
      rejected += flagged.size;
      const scope = referenceScope(
        fixtures[position.fixture],
        position.stepId,
        position.fieldPath,
      );
      expect(scope.found, JSON.stringify(position)).toBe(true);
      const accepted = candidates.filter((id) => !flagged.has(id)).sort();
      if (!scope.evaluated) {
        // The compiler never evaluates an output after a path that always fails.
        expect(flagged.size, JSON.stringify(position)).toBe(0);
        return;
      }
      expect(
        scope.steps.map((s) => s.id).sort(),
        JSON.stringify(position),
      ).toEqual(accepted);
      expect(
        scope.entries
          .filter((e) => e.path.length === 0 && e.source === "step")
          .map((e) => e.stepId)
          .sort(),
      ).toEqual(accepted);
    });
    expect(rejected).toBeGreaterThan(100);
  });
});
