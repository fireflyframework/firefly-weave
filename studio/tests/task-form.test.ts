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
import { describe, it, expect } from "vitest";
import {
  groupedObject,
  missingRequired,
  scalarList,
  Schema,
  withValue,
} from "../src/app/task-form";

const schema: Schema = {
  type: "object",
  required: ["parameters", "mode"],
  properties: {
    parameters: {
      type: "object",
      title: "Parameters",
      required: ["customerId"],
      properties: { customerId: { type: "string", title: "Customer ID" } },
    },
    mode: { type: "string", enum: ["fast", "safe"] },
    tags: { type: "array", items: { type: "string" } },
    rows: { type: "array", items: { type: "object" } },
    options: { type: "object" },
  },
};

describe("schema form helpers", () => {
  it("renders objects with properties and scalar lists without JSON", () => {
    expect(groupedObject(schema.properties!["parameters"])).toBe(true);
    expect(groupedObject(schema.properties!["options"])).toBe(false);
    expect(scalarList(schema.properties!["tags"])).toBe(true);
    expect(scalarList(schema.properties!["rows"])).toBe(false);
  });
  it("lists missing required values, including nested ones", () => {
    expect(missingRequired(schema, {})).toEqual(["Parameters", "mode"]);
    expect(missingRequired(schema, { parameters: {}, mode: "fast" })).toEqual([
      "Parameters › Customer ID",
    ]);
    expect(
      missingRequired(schema, {
        parameters: { customerId: "c-1" },
        mode: "safe",
      }),
    ).toEqual([]);
  });
  it("writes nested values and drops objects emptied by a removal", () => {
    const data = { parameters: { region: "eu" }, label: "x" };
    const written = withValue(data, ["parameters", "customerId"], "c-1");
    expect(written).toEqual({
      parameters: { region: "eu", customerId: "c-1" },
      label: "x",
    });
    expect(data).toEqual({ parameters: { region: "eu" }, label: "x" });
    expect(withValue(data, ["parameters", "region"], undefined)).toEqual({
      label: "x",
    });
    expect(
      withValue({ a: { b: { c: 1 } } }, ["a", "b", "c"], undefined),
    ).toEqual({});
    expect(withValue({ tags: [] }, ["tags"], [])).toEqual({ tags: [] });
  });
});
