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
import { StructuredCanvasAdapter } from "../src/app/model";
describe("structured workflow authoring", () => {
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
    m.editBranches("switch-1", "add");
    m.editBranches("switch-1", "up", "1");
    expect(
      m.nodes().find((n) => n.step.id === "switch-1")!.step[
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
      "switch-1/case 1",
      "parallel-1/first",
      "switch-1/default",
    ]);
    expect(m.error).toBe("");
    m.setSource(JSON.stringify(m.definition), "json");
    expect(m.nodes()).toHaveLength(4);
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
