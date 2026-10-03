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
import { StructuredCanvasAdapter } from "../src/app/model";
import {
  changeSlot,
  slotValidation,
} from "../src/app/integrations/connection-slots";

describe("connection slot edits", () => {
  it("renames nested uses in one undo without losing YAML comments", () => {
    const model = new StructuredCanvasAdapter();
    model.setSource(
      "# Preserve the business context\n" +
        readFileSync("tests/fixtures/vendor-payment-approval.yaml", "utf8"),
    );
    const before = model.source;
    changeSlot(model, "ledger-db", {
      name: "finance",
      connector: "weave-postgresql@1.0.0",
      required: true,
    });
    expect(
      model.nodes().filter((n) => n.step["connection"] === "finance"),
    ).toHaveLength(2);
    expect(model.definition.spec["connections"]).not.toHaveProperty(
      "ledger-db",
    );
    expect(model.source).toContain("# Preserve the business context");
    model.undo();
    expect(model.source).toBe(before);
  });
  it.each(["rename", "remove"])(
    "updates nested action and AI slots on %s with one undo",
    (operation) => {
      const model = new StructuredCanvasAdapter();
      model.setSource(`apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: nested, version: 1.0.0}
spec:
  connections: {ai: {connector: provider@1.0.0, required: true}}
  steps:
    - id: group
      kind: parallel
      branches:
        - name: first
          steps:
            - {id: remote, kind: action, uses: action@1.0.0, connection: ai}
        - name: second
          steps:
            - {id: generate, kind: llm, uses: action@1.0.0, connection: ai, profile: default, prompt: {literal: Hello}, context: {literal: {}}}
  output: {literal: {}}
`);
      const before = model.source;
      changeSlot(
        model,
        "ai",
        operation === "rename"
          ? { name: "provider", connector: "provider@1.0.0", required: true }
          : null,
      );
      for (const id of ["remote", "generate"]) {
        const step = model.nodes().find((node) => node.step.id === id)!.step;
        expect(step["connection"]).toBe(
          operation === "rename" ? "provider" : undefined,
        );
      }
      model.undo();
      expect(model.source).toBe(before);
    },
  );
  it.each([
    "db@1.0.0-",
    "db@1.0.0-01",
    "db@1.0.0+",
    "db@1.0.0+a..b",
    "db@01.0.0",
  ])("rejects invalid semantic version %s", (connector) => {
    expect(
      slotValidation({ name: "db", connector, required: true }, []),
    ).not.toBe("");
  });
  it("rejects collisions before changing the document", () => {
    const model = new StructuredCanvasAdapter();
    changeSlot(model, "", {
      name: "one",
      connector: "db@1.0.0",
      required: true,
    });
    changeSlot(model, "", {
      name: "two",
      connector: "db@1.0.0",
      required: true,
    });
    const before = model.source;
    expect(() =>
      changeSlot(model, "one", {
        name: "two",
        connector: "db@1.0.0",
        required: true,
      }),
    ).toThrow("already exists");
    expect(model.source).toBe(before);
  });
});
