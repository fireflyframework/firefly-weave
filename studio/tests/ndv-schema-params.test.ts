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
import { describe, expect, it } from "vitest";
import { paramsFromSchema } from "../src/app/editor/ndv/params/schema-params";
import { lookupAction } from "./browser/support";

const input = lookupAction.spec.inputSchema;

describe("fields from a schema", () => {
  it("shows required properties and offers optional ones", () => {
    const form = paramsFromSchema(input, ["with"], { idPrefix: "input." });
    expect(form.fields.map((f) => [f.id, f.type, f.label, f.required])).toEqual(
      [["input.parameters", "fields", "Parameters", true]],
    );
    expect(form.options?.map((f) => [f.id, f.type, f.label])).toEqual([
      ["input.limit", "number", "Limit"],
      ["input.mode", "select", "Mode"],
      ["input.label", "text", "Label"],
      ["input.tags", "list", "Tags"],
      ["input.dryRun", "boolean", "Dry run"],
      ["input.options", "keyValue", "Options"],
    ]);
  });
  it("maps every value field Fixed or Mapped at its data path", () => {
    const form = paramsFromSchema(input, ["with"], { idPrefix: "input." });
    const limit = form.options!.find((f) => f.id === "input.limit")!;
    expect(limit.path).toEqual(["with", "limit"]);
    expect(limit.mapping).toBe("both");
    expect(limit.min).toBe(1);
    const mode = form.options!.find((f) => f.id === "input.mode")!;
    expect(mode.choices).toEqual([
      { value: "fast", label: "fast" },
      { value: "safe", label: "safe" },
    ]);
  });
  it("nests an object's properties as children with their own paths", () => {
    const form = paramsFromSchema(input, ["with"], { idPrefix: "input." });
    const group = form.fields[0];
    const children = group.children!(
      { id: "s", kind: "action" } as never,
      undefined as never,
    );
    expect(children.map((c) => [c.id, c.path, c.required])).toEqual([
      [
        "input.parameters.customerId",
        ["with", "parameters", "customerId"],
        true,
      ],
      ["input.parameters.region", ["with", "parameters", "region"], false],
    ]);
  });
  it("types list items and file references", () => {
    const form = paramsFromSchema(
      {
        type: "object",
        required: ["to", "file"],
        properties: {
          to: { type: "array", items: { type: "string" } },
          file: {
            type: "object",
            properties: { kind: { const: "weave/file" } },
          },
        },
      },
      ["with"],
      { idPrefix: "" },
    );
    const [to, file] = form.fields;
    expect([to.type, to.item?.type, to.item?.path]).toEqual([
      "list",
      "text",
      [],
    ]);
    expect(file.type).toBe("fileRef");
  });
  it("gives each field of a list item its own ID", () => {
    const form = paramsFromSchema(
      {
        type: "object",
        required: ["lines"],
        properties: {
          lines: {
            type: "array",
            items: {
              type: "object",
              properties: { sku: { type: "string" }, qty: { type: "integer" } },
            },
          },
        },
      },
      ["with"],
      { idPrefix: "input." },
    );
    const item = form.fields[0].item!;
    expect([item.type, item.id]).toEqual(["fields", "input.lines.item"]);
    const children = item.children!(undefined as never, undefined as never);
    expect(children.map((c) => [c.id, c.path])).toEqual([
      ["input.lines.item.sku", ["sku"]],
      ["input.lines.item.qty", ["qty"]],
    ]);
  });
  it("shows an example as the placeholder only when there is no default", () => {
    const form = paramsFromSchema(
      {
        type: "object",
        required: ["code", "page"],
        properties: {
          code: { type: "string", examples: ["acme"] },
          page: { type: "integer", default: 1, examples: [5] },
        },
      },
      ["with"],
      { idPrefix: "" },
    );
    const [code, page] = form.fields;
    expect([code.placeholder, code.default]).toEqual(["acme", undefined]);
    expect([page.placeholder, page.default]).toEqual([undefined, 1]);
  });
});
