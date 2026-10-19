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
import { secretAt, type Schema } from "../src/app/forms/core/scope";

const SECRET = { type: "string", "x-secret": true };
const at = (schema: Schema, path: string) =>
  secretAt(schema, schema, path ? path.split(".") : []);

describe("finding values marked secret", () => {
  it("finds a secret field, a write-only one and anything inside a secret group", () => {
    const schema = {
      type: "object",
      properties: {
        password: SECRET,
        token: { type: "string", writeOnly: true },
        vault: {
          type: "object",
          "x-secret": true,
          properties: { a: { type: "string" } },
        },
        name: { type: "string" },
      },
    };
    expect(at(schema, "password")).toBe(true);
    expect(at(schema, "token")).toBe(true);
    expect(at(schema, "vault.a")).toBe(true);
    expect(at(schema, "vault.anything")).toBe(true);
    expect(at(schema, "name")).toBe(false);
    expect(at(schema, "")).toBe(false);
  });
  it("follows a local $ref to a secret", () => {
    const schema = {
      type: "object",
      $defs: {
        key: SECRET,
        box: { type: "object", properties: { key: { $ref: "#/$defs/key" } } },
      },
      properties: {
        key: { $ref: "#/$defs/key" },
        box: { $ref: "#/$defs/box" },
      },
    };
    expect(at(schema, "key")).toBe(true);
    expect(at(schema, "box.key")).toBe(true);
  });
  it("finds a secret in any branch of anyOf or oneOf, not only the first", () => {
    for (const word of ["anyOf", "oneOf"]) {
      const schema = {
        type: "object",
        properties: {
          login: {
            [word]: [
              { type: "object", properties: { user: { type: "string" } } },
              {
                type: "object",
                properties: { user: { type: "string" }, pin: SECRET },
              },
            ],
          },
          code: { [word]: [{ type: "string" }, SECRET] },
        },
      };
      expect(at(schema, "login.pin"), word).toBe(true);
      expect(at(schema, "code"), word).toBe(true);
      expect(at(schema, "login.user"), word).toBe(false);
    }
  });
  it("finds a secret that a later allOf part overrides", () => {
    const schema = {
      type: "object",
      allOf: [
        { type: "object", properties: { pin: SECRET } },
        { type: "object", properties: { pin: { type: "string" } } },
      ],
    };
    expect(at(schema, "pin")).toBe(true);
  });
  it("finds secrets in additionalProperties, patternProperties, prefixItems and items", () => {
    const schema = {
      type: "object",
      properties: {
        extra: { type: "object", additionalProperties: SECRET },
        named: { type: "object", patternProperties: { "^key-": SECRET } },
        pair: { type: "array", prefixItems: [{ type: "string" }, SECRET] },
        list: {
          type: "array",
          items: { type: "object", properties: { pin: SECRET } },
        },
        open: {
          type: "object",
          properties: { a: { type: "string" } },
          additionalProperties: { type: "string" },
        },
      },
    };
    expect(at(schema, "extra.x")).toBe(true);
    expect(at(schema, "named.key-1")).toBe(true);
    expect(at(schema, "named.other")).toBe(false);
    expect(at(schema, "pair.1")).toBe(true);
    expect(at(schema, "pair.0")).toBe(false);
    expect(at(schema, "list.3.pin")).toBe(true);
    expect(at(schema, "open.a")).toBe(false);
    expect(at(schema, "open.b")).toBe(false);
  });
  it("stays bounded on schemas that refer to themselves", () => {
    const schema = {
      type: "object",
      $defs: {
        node: {
          type: "object",
          properties: { next: { $ref: "#/$defs/node" }, pin: SECRET },
        },
      },
      properties: { root: { $ref: "#/$defs/node" } },
      anyOf: [{ $ref: "#" }],
    };
    expect(at(schema, "root.next.next.next.pin")).toBe(true);
    expect(at(schema, "root.next.next.other")).toBe(false);
  });
});
