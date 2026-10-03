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
import { rememberable } from "../src/app/run/run-memory";

describe("remembered run input", () => {
  it("leaves out secret and write-only values at any depth", () => {
    const schema = {
      type: "object",
      properties: {
        customerId: { type: "string" },
        apiKey: { type: "string", writeOnly: true },
        auth: {
          type: "object",
          properties: {
            user: { type: "string" },
            token: { type: "string", "x-secret": true },
          },
        },
      },
    };
    expect(
      rememberable(schema, {
        customerId: "C-1",
        apiKey: "k",
        auth: { user: "ana", token: "t" },
        extra: 1,
      }),
    ).toEqual({ customerId: "C-1", auth: { user: "ana" }, extra: 1 });
  });
  it("keeps nothing it can't classify when a secret hides behind composition", () => {
    const viaRef = {
      type: "object",
      $defs: { secret: { type: "string", writeOnly: true } },
      properties: {
        token: { $ref: "#/$defs/secret" },
        note: { type: "string" },
      },
    };
    expect(rememberable(viaRef, { token: "t", note: "n" })).toEqual({});
    const viaItems = {
      type: "object",
      properties: {
        keys: { type: "array", items: { type: "string", "x-secret": true } },
        note: { type: "string" },
      },
    };
    expect(rememberable(viaItems, { keys: ["a"], note: "n" })).toEqual({
      note: "n",
    });
    const viaAdditional = {
      type: "object",
      properties: { note: { type: "string" } },
      additionalProperties: { type: "string", writeOnly: true },
    };
    expect(rememberable(viaAdditional, { note: "n", other: "s" })).toEqual({});
  });
  it("keeps everything when nothing is secret", () => {
    expect(rememberable({ type: "object" }, { a: { b: [1] } })).toEqual({
      a: { b: [1] },
    });
  });
});
