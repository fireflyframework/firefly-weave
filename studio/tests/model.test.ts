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
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { parseDocument } from "yaml";
import {
  StructuredCanvasAdapter,
  freshWorkflow,
  ownerLabel,
  type Kind,
} from "../src/app/model";
describe("structured workflow authoring", () => {
  it("renames a human answer and matching nested decision conditions in one undo", () => {
    const model = new StructuredCanvasAdapter();
    const task = model.insert("humanTask");
    const choice = model.branchOnDecision(task.id);
    const nested = model.insert("wait", `${choice.id}/case 1`);
    const before = model.source;
    expect(model.renameDecision(task.id, "approve", "accept")).toBe(1);
    expect(
      model.nodes().find((node) => node.step.id === task.id)!.step["decisions"],
    ).toEqual(["accept", "reject"]);
    const renamed = model.nodes().find((node) => node.step.id === choice.id)!
      .step["cases"] as any[];
    expect(renamed[0].when.op.args[1]).toEqual({ literal: "accept" });
    expect(renamed[0].steps[0].id).toBe(nested.id);
    expect(renamed[0].output).toEqual({ literal: {} });
    model.undo();
    expect(model.source).toBe(before);
    expect(() => model.renameDecision(task.id, "approve", "reject")).toThrow();
    expect(() =>
      model.renameDecision(task.id, "approve", "bad name"),
    ).toThrow();
    expect(model.source).toBe(before);
  });
  it("groups an immediate derived update with its initiating edit, fencing later edits", () => {
    const model = new StructuredCanvasAdapter();
    const action = model.insert("action");
    const revision = model.revision;
    model.continueEdit(revision, () =>
      model.update(
        action.id,
        JSON.stringify({ ...action, connection: "primary" }),
      ),
    );
    model.undo();
    expect(model.definition.spec.steps).toHaveLength(0);
    model.redo();
    const prior = model.revision;
    model.insert("wait");
    model.continueEdit(prior, () =>
      model.update(
        action.id,
        JSON.stringify({ ...action, connection: "other" }),
      ),
    );
    model.undo();
    expect(model.definition.spec.steps).toHaveLength(2);
    expect(model.definition.spec.steps[0]["connection"]).toBe("primary");
  });
  it("keeps the rendered node identity stable until rename teardown", () => {
    const model = new StructuredCanvasAdapter();
    const step = model.insert("wait");
    const rendered = model.nodes()[0];
    model.renameStep(step.id, "pause");
    expect(rendered.step.id).toBe("wait-1");
    expect(model.nodes()[0].step.id).toBe("pause");
    model.undo();
    expect(model.nodes()[0].step.id).toBe("wait-1");
  });
  it("round trips decision tables and AI tasks without rewriting their expressions", () => {
    const m = new StructuredCanvasAdapter();
    const decision = m.insert("decisionTable" as Kind);
    const ai = m.insert("llm" as Kind);
    expect(decision.kind).toBe("decisionTable");
    expect(decision["with"]).toEqual({ ref: "/input" });
    expect(ai["prompt"]).toEqual({ literal: "" });
    expect(m.readonly).toBe(false);
    const copy = new StructuredCanvasAdapter();
    copy.setSource(m.source);
    expect(copy.definition.spec.steps).toEqual(m.definition.spec.steps);
    m.update(
      ai.id,
      JSON.stringify({
        ...ai,
        prompt: { ref: `/steps/${decision.id}/output` },
      }),
    );
    expect(
      m
        .referenceSites()
        .filter((site) => site.ref === `/steps/${decision.id}/output`),
    ).toHaveLength(1);
  });
  it("branch management preserves nested steps and rejects unsafe removal", () => {
    const m = new StructuredCanvasAdapter();
    m.insert("parallel");
    m.insert("wait", "parallel-1/first");
    expect(() => m.editBranches("parallel-1", "remove", "first")).toThrow(
      "contained steps",
    );
    m.editBranches("parallel-1", "rename", "first", "primary");
    expect(m.nodes().find((n) => n.step.id === "wait-1")?.owner).toBe(
      "parallel-1/primary",
    );
    m.editBranches("parallel-1", "add");
    expect(Object.keys(m.nodes()[0].step["branches"] as object)).toHaveLength(
      3,
    );
    m.editBranches("parallel-1", "remove", "branch-1");
    expect(Object.keys(m.nodes()[0].step["branches"] as object)).toHaveLength(
      2,
    );
    m.insert("switch");
    m.editBranches("decision-1", "add");
    m.editBranches("decision-1", "up", "1");
    expect(
      m.nodes().find((n) => n.step.id === "decision-1")!.step[
        "cases"
      ] as unknown[],
    ).toHaveLength(2);
  });
  it("renders start/end connections without synthetic execution steps", () => {
    const m = new StructuredCanvasAdapter();
    expect(m.visualConnections()).toEqual([{ from: "$start", to: "$end" }]);
    m.insert("parallel");
    m.insert("wait", "parallel-1/first");
    m.insert("transform", "parallel-1/second");
    expect(m.visualConnections()).toEqual([
      { from: "$start", to: "parallel-1" },
      { from: "parallel-1", to: "wait-1" },
      { from: "parallel-1", to: "transform-1" },
      { from: "wait-1", to: "$end" },
      { from: "transform-1", to: "$end" },
    ]);
    expect(m.source).not.toContain("$start");
    expect(m.source).not.toContain("$end");
    expect(m.boundaries().end.y).toBeGreaterThan(
      Math.max(...m.nodes().map((n) => n.point.y)),
    );
  });
  it("workflow options retain steps, comments and layout", () => {
    const m = new StructuredCanvasAdapter();
    m.insert("wait");
    m.setSource("# Keep me\n" + m.source);
    m.position("wait-1", { x: 112, y: 224 });
    const layout = structuredClone(m.layout);
    const doc = structuredClone(m.definition);
    doc.metadata.version = "2.0.0";
    doc.spec["timeoutSeconds"] = 120;
    doc.spec.steps = [];
    m.updateWorkflow(doc);
    expect(m.definition.spec.steps).toHaveLength(1);
    expect(m.source).toContain("# Keep me");
    expect(m.layout).toEqual(layout);
  });
  it("renames without losing comments or sidecar and rejects invalid source", () => {
    const m = new StructuredCanvasAdapter();
    m.insert("wait");
    m.setSource("# preserved\n" + m.source);
    m.position("wait-1", { x: 112, y: 224 });
    const layout = structuredClone(m.layout);
    m.renameWorkflow("renamed");
    expect(m.source).toContain("# preserved");
    expect(m.layout).toEqual(layout);
    expect(m.definition.metadata.name).toBe("renamed");
    m.setSource("invalid: [");
    expect(() => m.renameWorkflow("lost")).toThrow();
    expect(m.source).toBe("invalid: [");
  });
  it("preserves step comments across reorder and refuses YAML disguised as JSON", () => {
    const m = new StructuredCanvasAdapter();
    m.insert("wait");
    m.setSource(
      m.source.replace(
        "durationSeconds: 60",
        "durationSeconds: 60 # elapsed seconds",
      ),
    );
    m.insert("transform");
    m.move("wait-1", "root", 2);
    expect(m.source).toContain("# elapsed seconds");
    m.setSource(m.source, "json");
    expect(m.readonly).toBe(true);
  });

  it("preserves switch and parallel branch ownership across YAML/JSON", () => {
    const m = new StructuredCanvasAdapter();
    const decision = m.insert("switch");
    m.insert("parallel", `${decision.id}/case 1`);
    const group = m.nodes().find((n) => n.step.kind === "parallel")!;
    m.insert("wait", `${group.step.id}/first`);
    m.insert("transform", `${decision.id}/default`);
    const source = m.source;
    m.setSource(source);
    expect(m.nodes().map((n) => n.owner)).toEqual([
      "root",
      "decision-1/case 1",
      "parallel-1/first",
      "decision-1/default",
    ]);
    expect(m.error).toBe("");
    m.setSource(JSON.stringify(m.definition), "json");
    expect(m.nodes()).toHaveLength(4);
  });
  it("reports deeply nested YAML through the parser error boundary and preserves the workflow", () => {
    const source = `payload: ${"[".repeat(5000)}1${"]".repeat(5000)}`;
    // Source editing reads Document.errors; exhausting the parser must use that boundary.
    const document = parseDocument(source);
    expect(
      document.errors.some((error) => error.code === "RESOURCE_EXHAUSTION"),
    ).toBe(true);

    const model = new StructuredCanvasAdapter();
    model.insert("wait");
    const previousSource = model.source;
    const previousDefinition = structuredClone(model.definition);
    model.setSource(source);
    expect(model.readonly).toBe(true);
    expect(model.error).not.toBe("");
    expect(model.source).toBe(source);
    expect(model.definition).toEqual(previousDefinition);
    model.undo();
    expect(model.source).toBe(previousSource);
    expect(model.definition).toEqual(previousDefinition);
    expect(model.readonly).toBe(false);
    expect(model.error).toBe("");
  });
  it("keeps the last valid graph and exact invalid or unsupported source", () => {
    const m = new StructuredCanvasAdapter();
    m.insert("wait");
    const graph = JSON.stringify(m.definition);
    m.setSource("spec: [ invalid");
    expect(m.source).toBe("spec: [ invalid");
    expect(m.readonly).toBe(true);
    expect(JSON.stringify(m.definition)).toBe(graph);
    const unsupported = (m.source = JSON.stringify({
      ...m.definition,
      spec: { ...m.definition.spec, steps: [{ id: "new", kind: "future" }] },
    }));
    m.setSource(unsupported, "json");
    expect(m.source).toBe(unsupported);
    expect(JSON.stringify(m.definition)).toBe(graph);
  });
  it("rejects cycles and keeps populated groups safe from accidental deletion", () => {
    const m = new StructuredCanvasAdapter();
    const group = m.insert("parallel");
    m.insert("wait", `${group.id}/first`);
    const before = m.source;
    expect(() => m.move(group.id, `${group.id}/first`, 0)).toThrow();
    expect(() => m.remove(group.id)).toThrow();
    expect(m.source).toBe(before);
  });
  it("moves layout without changing source semantics and groups undo by gesture", () => {
    const m = new StructuredCanvasAdapter();
    m.insert("wait");
    const source = m.source;
    m.position("wait-1", { x: 113, y: 222 });
    expect(m.source).toBe(source);
    expect(m.layout.positions["wait-1"]).toEqual({ x: 112, y: 224 });
    m.undo();
    expect(m.layout.positions).toEqual({});
    expect(m.source).toBe(source);
    m.redo();
    expect(m.layout.positions["wait-1"].y).toBe(224);
  });
  it("requires explicit placement and excludes unplaced draft nodes from execution", () => {
    const m = new StructuredCanvasAdapter();
    m.addUnplaced("transform");
    expect(m.definition.spec.steps).toHaveLength(0);
    m.place(m.unplaced[0].id, "root", 0);
    expect(m.unplaced).toHaveLength(0);
    expect(m.definition.spec.steps).toHaveLength(1);
  });
  it("does not leave references dangling on deletion", () => {
    const m = new StructuredCanvasAdapter();
    m.insert("wait");
    const b = m.insert("transform");
    m.update(
      b.id,
      JSON.stringify({ ...b, value: { ref: "/steps/wait-1/output" } }),
    );
    expect(() => m.remove("wait-1")).toThrow("reference");
  });
  it("preserves document comments outside the changed steps subtree", () => {
    const m = new StructuredCanvasAdapter();
    m.setSource("# A workflow\n" + m.source);
    m.insert("wait");
    expect(m.source).toContain("# A workflow");
  });
});

describe("designer safety and canvas reachability", () => {
  it("opening another workflow starts a fresh undo history", () => {
    const m = new StructuredCanvasAdapter();
    m.insert("wait");
    expect(m.canUndo).toBe(true);
    const next = freshWorkflow();
    next.metadata.name = "opened";
    m.replace(next);
    expect(m.canUndo).toBe(false);
    expect(m.canRedo).toBe(false);
    m.undo();
    expect(m.definition.metadata.name).toBe("opened");
    m.insert("transform");
    m.clearHistory();
    expect(m.canUndo).toBe(false);
  });
  it("anchors empty branches next to their group with unique labels", () => {
    const m = new StructuredCanvasAdapter();
    m.insert("wait");
    const decision = m.insert("switch");
    m.editBranches(decision.id, "add");
    const group = m.insert("parallel");
    const targets = m.targets();
    const empty = targets.filter((t) => t.empty);
    expect(empty.map((t) => t.label)).toEqual([
      "Add a step here, in Case 1 of decision-1",
      "Add a step here, in Case 2 of decision-1",
      "Add a step here, in Otherwise of decision-1",
      "Add a step here, in first of parallel-1",
      "Add a step here, in second of parallel-1",
    ]);
    // The cards name each path by its condition, "Otherwise" for the rest.
    expect(empty.map((t) => t.empty)).toEqual([
      "Condition not set",
      "Condition not set",
      "Otherwise",
      "first",
      "second",
    ]);
    expect(new Set(targets.map((t) => t.label)).size).toBe(targets.length);
    const points = targets.map((t) => `${t.point.x},${t.point.y}`);
    expect(new Set(points).size).toBe(points.length);
    const nodes = m.nodes();
    for (const target of empty) {
      const parent = nodes.find((n) =>
        target.owner.startsWith(n.step.id + "/"),
      )!;
      const center = { x: parent.point.x + 104, y: parent.point.y + 32 };
      expect(
        Math.hypot(target.point.x - center.x, target.point.y - center.y),
        target.label,
      ).toBeLessThan(400);
    }
    // Placeholders never overlap a step card or another placeholder.
    const cards = [
      ...nodes.map((n) => ({ ...n.point, h: 64 })),
      ...m.placeholders().map((p) => ({ ...p.point, h: 48 })),
    ];
    for (const a of cards)
      for (const b of cards)
        if (a !== b)
          expect(
            a.x + 208 <= b.x ||
              b.x + 208 <= a.x ||
              a.y + a.h <= b.y ||
              b.y + b.h <= a.y,
          ).toBe(true);
    expect(m.boundaries().end.y).toBeGreaterThan(
      Math.max(...m.placeholders().map((p) => p.point.y)),
    );
    // A round "+" never covers a placeholder card or another "+".
    m.insert("wait", "decision-1/default");
    const after = m.targets();
    const boxes = after.map((t) =>
      t.empty
        ? { x: t.point.x - 104, y: t.point.y - 24, w: 208, h: 48 }
        : { x: t.point.x - 18, y: t.point.y - 18, w: 36, h: 36 },
    );
    for (const a of boxes)
      for (const b of boxes)
        if (a !== b)
          expect(
            a.x + a.w <= b.x ||
              b.x + b.w <= a.x ||
              a.y + a.h <= b.y ||
              b.y + b.h <= a.y,
          ).toBe(true);
    m.undo();
    // Inserting through the empty-branch target lands in that branch.
    const second = empty[1];
    m.insert("transform", second.owner, second.index);
    const cases = m.definition.spec.steps[1]["cases"] as {
      steps: { id: string }[];
    }[];
    expect(cases[1].steps.map((s) => s.id)).toEqual(["transform-1"]);
    expect(group.id).toBe("parallel-1");
  });
  it("draws empty branches through their placeholder only on the designer canvas", () => {
    const m = new StructuredCanvasAdapter();
    m.insert("parallel");
    m.insert("wait", "parallel-1/first");
    expect(m.visualConnections(true)).toEqual([
      { from: "$start", to: "parallel-1" },
      { from: "parallel-1", to: "$split:parallel-1" },
      { from: "$split:parallel-1", to: "wait-1" },
      { from: "$split:parallel-1", to: "$empty:parallel-1/second" },
      { from: "$empty:parallel-1/second", to: "$join:parallel-1" },
      { from: "$join:parallel-1", to: "$end" },
      { from: "wait-1", to: "$join:parallel-1" },
    ]);
    expect(m.visualConnections()).toContainEqual({
      from: "parallel-1",
      to: "$end",
    });
  });
  it("labels non-empty branch targets in plain words", () => {
    const m = new StructuredCanvasAdapter();
    m.insert("switch");
    m.insert("wait", "decision-1/case 1");
    const labels = m.targets().map((t) => t.label);
    expect(labels).toContain("Add a step here, at the start");
    expect(labels).toContain(
      "Add a step here, at the start of Case 1 of decision-1",
    );
    expect(labels).toContain("Add a step after wait-1, in Case 1");
    expect(labels).toContain("Add a step here, after decision-1");
  });
  it("inserts a step with initial fields in one undo step", () => {
    const m = new StructuredCanvasAdapter();
    const step = m.insert("action", "root", 0, { uses: "crm.lookup@1.0.0" });
    expect(step["uses"]).toBe("crm.lookup@1.0.0");
    expect(m.source).toContain("uses: crm.lookup@1.0.0");
    m.undo();
    expect(m.definition.spec.steps).toHaveLength(0);
  });
});

describe("step identity", () => {
  const document = `# Onboarding
apiVersion: weave/v1alpha1
kind: Workflow
metadata:
  name: onboarding
  version: 1.0.0
spec:
  inputSchema: { type: object }
  outputSchema: { type: object }
  steps:
    - id: action-1 # looks up the customer
      kind: action
      uses: crm.lookup@1.0.0
      with:
        object:
          customerId: { ref: /input/customerId }
    - id: decide
      kind: switch
      cases:
        - when:
            op:
              name: eq
              args:
                - ref: /steps/action-1/output/status # status from CRM
                - literal: active
          steps:
            - id: note
              kind: transform
              value:
                object:
                  whole: { ref: /steps/action-1 }
                  text: { literal: /steps/action-1/output/x }
                  same: { literal: { ref: /steps/action-1/output } }
          output: { ref: /steps/note/output }
      default: { steps: [], output: { literal: {} } }
  output:
    object:
      customer: { ref: /steps/action-1/output }
      other: { ref: /steps/action-10/output }
`;
  it("renames a step and rewrites only real references, keeping comments", () => {
    const m = new StructuredCanvasAdapter();
    m.setSource(document);
    m.position("action-1", { x: 400, y: 400 });
    m.selected = "action-1";
    expect(m.renameStep("action-1", "lookup")).toBe(3);
    const text = m.source;
    expect(text).toContain("# Onboarding");
    expect(text).toContain("# looks up the customer");
    expect(text).toContain("# status from CRM");
    expect(text).toContain("id: lookup");
    expect(text).toContain("ref: /steps/lookup/output/status");
    expect(text).toContain("whole: { ref: /steps/lookup }");
    expect(text).toContain("customer: { ref: /steps/lookup/output }");
    // Literal text and similarly named steps are not references.
    expect(text).toContain("text: { literal: /steps/action-1/output/x }");
    expect(text).toContain(
      "same: { literal: { ref: /steps/action-1/output } }",
    );
    expect(text).toContain("other: { ref: /steps/action-10/output }");
    expect(m.layout.positions["lookup"]).toEqual({ x: 400, y: 400 });
    expect(m.layout.positions["action-1"]).toBeUndefined();
    expect(m.selected).toBe("lookup");
    m.undo();
    expect(m.source).toBe(document);
  });
  it("rejects invalid or duplicate step names", () => {
    const m = new StructuredCanvasAdapter();
    m.setSource(document);
    expect(() => m.renameStep("action-1", "note")).toThrow("already");
    expect(() => m.renameStep("action-1", "has space")).toThrow("letters");
    expect(() => m.renameStep("action-1", "")).toThrow("letters");
    expect(m.source).toBe(document);
    m.setSource(JSON.stringify(m.definition, null, 2), "json");
    m.renameStep("note", "summary");
    expect(JSON.parse(m.source).spec.steps[1].cases[0].output).toEqual({
      ref: "/steps/summary/output",
    });
  });
  it("names the steps that still read a step before deleting it", () => {
    const m = new StructuredCanvasAdapter();
    m.setSource(document);
    expect(() => m.remove("action-1")).toThrow(
      "This step is referenced by decide, note and the workflow output.",
    );
    expect(() => m.remove("decide")).toThrow("contained");
    expect(m.containedSteps("decide")).toBe(1);
    // Removing the group with its contents is allowed: only its own steps read note.
    m.remove("decide", { contents: true });
    expect(m.nodes().map((n) => n.step.id)).toEqual(["action-1"]);
  });
  it("blocks deleting a group whose steps are read from outside it", () => {
    const m = new StructuredCanvasAdapter();
    m.insert("parallel");
    m.insert("wait", "parallel-1/first");
    const t = m.insert("transform");
    m.update(
      t.id,
      JSON.stringify({ ...t, value: { ref: "/steps/wait-1/output" } }),
    );
    expect(() => m.remove("parallel-1", { contents: true })).toThrow(
      "Steps in this group are referenced by transform-1.",
    );
  });
  it("branches on a human task's decisions", () => {
    const m = new StructuredCanvasAdapter();
    m.insert("humanTask");
    m.insert("wait");
    const decision = m.branchOnDecision("approval-1");
    expect(m.definition.spec.steps.map((s) => s.id)).toEqual([
      "approval-1",
      decision.id,
      "wait-1",
    ]);
    expect(decision.kind).toBe("switch");
    expect(decision["cases"]).toEqual([
      {
        when: {
          op: {
            name: "eq",
            args: [
              { ref: "/steps/approval-1/output/decision" },
              { literal: "approve" },
            ],
          },
        },
        steps: [],
        output: { literal: {} },
      },
      {
        when: {
          op: {
            name: "eq",
            args: [
              { ref: "/steps/approval-1/output/decision" },
              { literal: "reject" },
            ],
          },
        },
        steps: [],
        output: { literal: {} },
      },
    ]);
    expect(m.selected).toBe(decision.id);
    m.undo();
    expect(m.definition.spec.steps).toHaveLength(2);
  });
  it("branches on approve and reject when a human task lists no decisions", () => {
    const m = new StructuredCanvasAdapter();
    const task = m.insert("humanTask");
    delete task["decisions"];
    const decision = m.branchOnDecision("approval-1");
    expect(
      (decision["cases"] as { when: { op: { args: unknown[] } } }[]).map(
        (c) => c.when.op.args[1],
      ),
    ).toEqual([{ literal: "approve" }, { literal: "reject" }]);
  });
});

describe("designer naming and editing", () => {
  it("names new steps readably; existing IDs stay as they are", () => {
    const m = new StructuredCanvasAdapter();
    const ids = [
      "action",
      "transform",
      "switch",
      "parallel",
      "wait",
      "signal",
      "humanTask",
      "fail",
    ].map((kind) => m.insert(kind as Kind).id);
    expect(ids).toEqual([
      "call-action-1",
      "transform-1",
      "decision-1",
      "parallel-1",
      "wait-1",
      "signal-1",
      "approval-1",
      "fail-1",
    ]);
    expect(m.insert("switch").id).toBe("decision-2");
    expect(ownerLabel("root")).toBe("Main sequence");
    expect(ownerLabel("route/case 1")).toBe("Case 1 of route");
    expect(ownerLabel("route/default")).toBe("Otherwise of route");
    expect(ownerLabel("fulfil/first")).toBe("first of fulfil");
  });
  it("starts a new decision case without a condition", () => {
    const m = new StructuredCanvasAdapter();
    const decision = m.insert("switch");
    m.editBranches(decision.id, "add");
    const cases = m.nodes()[0].step["cases"] as { when?: unknown }[];
    expect(cases).toHaveLength(2);
    expect(cases.every((c) => c.when === undefined)).toBe(true);
    expect(m.source).not.toContain("literal: true");
  });
  it("keeps the selection when another step is deleted", () => {
    const m = new StructuredCanvasAdapter();
    m.insert("wait");
    m.insert("transform");
    m.selected = "transform-1";
    m.remove("wait-1");
    expect(m.selected).toBe("transform-1");
    m.remove("transform-1");
    expect(m.selected).toBe("");
  });
  it("counts edits, so a late Undo knows the workflow changed since", () => {
    const m = new StructuredCanvasAdapter();
    const start = m.revision;
    m.insert("wait");
    expect(m.revision).toBe(start + 1);
    m.undo();
    expect(m.revision).toBe(start + 2);
    m.redo();
    expect(m.revision).toBe(start + 3);
  });
  it("duplicates a step with its contents under new names, as one undo step", () => {
    const m = new StructuredCanvasAdapter();
    m.insert("parallel");
    m.insert("wait", "parallel-1/first");
    const reader = m.insert("transform", "parallel-1/second");
    m.update(
      reader.id,
      JSON.stringify({ ...reader, value: { ref: "/steps/wait-1/output" } }),
    );
    m.insert("transform");
    const before = m.revision;
    const copy = m.duplicate("parallel-1");
    expect(m.revision).toBe(before + 1);
    expect(copy).toBe("parallel-2");
    expect(m.definition.spec.steps.map((s) => s.id)).toEqual([
      "parallel-1",
      "parallel-2",
      "transform-2",
    ]);
    const branches = m.definition.spec.steps[1]["branches"] as Record<
      string,
      { steps: { id: string; value?: unknown }[] }
    >;
    expect(branches["first"].steps[0].id).toBe("wait-2");
    // Inside the copy, a reference to a copied step follows the copy.
    expect(branches["second"].steps[0]).toMatchObject({
      id: "transform-3",
      value: { ref: "/steps/wait-2/output" },
    });
    expect(m.selected).toBe("parallel-2");
    m.undo();
    expect(m.definition.spec.steps.map((s) => s.id)).toEqual([
      "parallel-1",
      "transform-2",
    ]);
  });
});

describe("fixture insertion target ownership", () => {
  it("gives every insertion place its own hit target on the fixture", () => {
    const model = new StructuredCanvasAdapter();
    model.setSource(
      readFileSync(
        new URL("./fixtures/vendor-payment-approval.yaml", import.meta.url),
        "utf8",
      ),
    );
    const targets = model.targets();
    const boxes = targets.map((target) => ({
      ...target,
      x: target.point.x - (target.empty ? 104 : 12),
      y: target.point.y - (target.empty ? 24 : 12),
      width: target.empty ? 208 : 24,
      height: target.empty ? 48 : 24,
    }));
    for (let i = 0; i < boxes.length; i++) {
      for (let j = i + 1; j < boxes.length; j++) {
        const a = boxes[i],
          b = boxes[j];
        expect(
          a.x + a.width <= b.x ||
            b.x + b.width <= a.x ||
            a.y + a.height <= b.y ||
            b.y + b.height <= a.y,
          `${a.owner}:${a.index} overlaps ${b.owner}:${b.index}`,
        ).toBe(true);
      }
    }
    expect(
      targets.find(
        (target) =>
          target.owner === "pay-and-notify/ledger" && target.index === 1,
      )?.label,
    ).toBe("Add a step after post-ledger-entry, in ledger");
  });
});
describe("runs of steps", () => {
  const source = `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: runs, version: 1.0.0}
spec:
  inputSchema: {type: object}
  outputSchema: {type: object}
  steps:
    - {id: load, kind: transform, value: {ref: /input}}
    - {id: shape, kind: transform, value: {ref: /steps/load/output}}
    - {id: finish, kind: transform, value: {ref: /steps/shape/output}}
  output: {ref: /steps/finish/output}
`;
  const open = (text = source) => {
    const model = new StructuredCanvasAdapter();
    model.setSource(text);
    return model;
  };
  const ids = (model: StructuredCanvasAdapter) =>
    model.definition.spec.steps.map((step) => step.id);

  it("duplicates a run right after itself, rewriting references between the copies, as one undo step", () => {
    const model = open();
    const before = model.revision;
    expect(model.duplicateRun(["shape", "load"])).toEqual([
      "load-1",
      "shape-1",
    ]);
    expect(ids(model)).toEqual([
      "load",
      "shape",
      "load-1",
      "shape-1",
      "finish",
    ]);
    expect(model.definition.spec.steps[3]["value"]).toEqual({
      ref: "/steps/load-1/output",
    });
    expect(model.definition.spec.steps[2]["value"]).toEqual({ ref: "/input" });
    expect(model.selected).toBe("load-1");
    expect(model.revision).toBe(before + 1);
    model.undo();
    expect(ids(model)).toEqual(["load", "shape", "finish"]);
  });

  it("refuses a run with a gap, and an empty one", () => {
    const model = open();
    expect(() => model.duplicateRun(["load", "finish"])).toThrow(
      "Select steps next to each other in one path.",
    );
    expect(() => model.duplicateRun([])).toThrow("Select a placed step.");
    expect(ids(model)).toEqual(["load", "shape", "finish"]);
  });

  it("still duplicates one step the way it always did", () => {
    const model = open();
    expect(model.duplicate("shape")).toBe("shape-1");
    expect(ids(model)).toEqual(["load", "shape", "shape-1", "finish"]);
  });

  it("deletes several steps as one undo step", () => {
    const model = open(
      source.replace(
        "output: {ref: /steps/finish/output}",
        "output: {literal: {}}",
      ),
    );
    const before = JSON.stringify(model.definition.spec.steps);
    model.removeSteps(["finish", "shape"]);
    expect(ids(model)).toEqual(["load"]);
    model.undo();
    expect(JSON.stringify(model.definition.spec.steps)).toBe(before);
  });

  it("refuses to delete steps something else still reads, and names what reads them", () => {
    const model = open();
    expect(() => model.removeSteps(["shape", "finish"])).toThrow(
      "These steps are referenced by the workflow output. Update it before deleting them.",
    );
    expect(() => model.removeSteps(["load", "shape"])).toThrow(
      "These steps are referenced by finish. Update it before deleting them.",
    );
    expect(ids(model)).toEqual(["load", "shape", "finish"]);
  });

  it("refuses to delete steps while the source does not parse, and changes nothing", () => {
    const model = open(
      source.replace(
        "output: {ref: /steps/finish/output}",
        "output: {literal: {}}",
      ),
    );
    model.setSource("spec: [");
    expect(model.readonly).toBe(true);
    const revision = model.revision;
    const steps = JSON.stringify(model.definition.spec.steps);
    expect(() => model.removeSteps(["finish"])).toThrow(
      "Fix source before editing the graph.",
    );
    expect(model.revision).toBe(revision);
    expect(JSON.stringify(model.definition.spec.steps)).toBe(steps);
  });

  it("deletes a group with the steps inside it, even when both are listed", () => {
    const model = new StructuredCanvasAdapter();
    model.insert("switch");
    model.insert("wait", "decision-1/default", 0);
    model.removeSteps(["decision-1", "wait-1"]);
    expect(model.definition.spec.steps).toEqual([]);
  });
});
