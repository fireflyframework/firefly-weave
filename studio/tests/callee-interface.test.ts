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
// A called workflow's input and result, from the project's catalog lock or a
// draft on this computer, and a call step's output as the compiler infers it
// (tests/unit/test_callable_corpus.py checks the same output cases against
// the platform's call_output_schema).
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import type { LocalCallee } from "../src/app/editor/calls/callable-catalog";
import {
  callOutputSchema,
  findCallee,
} from "../src/app/editor/calls/callee-interface";
import {
  parseCatalogLock,
  type CatalogLockState,
} from "../src/app/editor/calls/catalog-lock";

const corpus = JSON.parse(
  readFileSync(
    resolve(import.meta.dirname, "fixtures/callable-corpus.json"),
    "utf8",
  ),
) as {
  lists: { lock: unknown; drafts: LocalCallee[] }[];
  outputs: {
    name: string;
    mode: string;
    onFailure?: string;
    callee: Record<string, unknown>;
    output: Record<string, unknown>;
  }[];
};
const [PROJECT] = corpus.lists;
const READY: CatalogLockState = {
  kind: "ready",
  lock: parseCatalogLock(PROJECT.lock)!,
};
const find = (uses: string, lock: CatalogLockState = READY) =>
  findCallee(uses, { lock, drafts: PROJECT.drafts });
const NOTIFY_OUTPUT = {
  type: "object",
  properties: { sent: { type: "boolean" } },
  required: ["sent"],
};

describe("a call step's output", () => {
  it.each(corpus.outputs)("$name", ({ mode, onFailure, callee, output }) => {
    expect(callOutputSchema(mode, onFailure, callee)).toEqual(output);
  });

  it("is any value while the called workflow's result is unknown", () => {
    expect(callOutputSchema("wait", undefined, null)).toEqual({});
    expect(callOutputSchema("wait", "continue", null)).toMatchObject({
      properties: { output: {} },
      required: ["status", "runId"],
    });
  });

  it("waits when the step names no mode", () => {
    expect(callOutputSchema(undefined, undefined, NOTIFY_OUTPUT)).toEqual(
      NOTIFY_OUTPUT,
    );
  });
});

describe("a called workflow's interface", () => {
  it("reads a published version's input, result and callable from the lock", () => {
    const callee = find("notify-customer@1.10.0");
    expect(callee.state).toBe("found");
    expect(callee.state === "found" && callee.source).toBe("project");
    expect(callee.contract?.inputSchema).toMatchObject({
      required: ["customerId"],
    });
    expect(callee.contract?.outputSchema).toEqual(NOTIFY_OUTPUT);
    expect(callee.contract?.callable).toEqual({
      allowedCallers: ["order-intake"],
    });
    expect(callee.contract?.source).toBe("project");
  });

  it("hands over the project's shared schemas a version's schemas may reference", () => {
    const lock: CatalogLockState = {
      kind: "ready",
      lock: { ...parseCatalogLock(PROJECT.lock)!, schemas: { "a.json": {} } },
    };
    expect(find("notify-customer@1.10.0", lock).contract?.schemas).toEqual({
      "a.json": {},
    });
    expect(find("lookup-customer@0.1.0", lock).contract?.schemas).toEqual({
      "a.json": {},
    });
    expect(
      find("lookup-customer@0.1.0", { kind: "offline" }).contract?.schemas,
    ).toEqual({});
  });

  it("reads a draft on this computer from its document", () => {
    const callee = find("lookup-customer@0.1.0");
    expect(callee.state === "found" && callee.source).toBe("local");
    expect(callee.contract).toEqual({
      inputSchema: { type: "object" },
      outputSchema: { type: "object" },
      callable: { allowedCallers: ["order-intake"] },
      source: "local",
      schemas: {},
    });
  });

  it("takes the published version when a draft repeats its reference", () => {
    const callee = find("notify-customer@1.0.0");
    expect(callee.state === "found" && callee.source).toBe("project");
    expect(callee.contract?.callable).toEqual({});
  });

  it("keeps a draft it can't read, with no input or result", () => {
    const callee = find("broken-draft@0.1.0");
    expect(callee.state === "found" && callee.source).toBe("local");
    expect(callee.contract).toEqual({
      inputSchema: null,
      outputSchema: null,
      callable: null,
      source: "local",
      schemas: {},
    });
  });

  it("answers null when the project doesn't have the version", () => {
    expect(find("notify-customer@3.0.0")).toEqual({
      state: "missing",
      contract: null,
      message: "Studio can't find notify-customer 3.0.0 in this project.",
    });
  });

  it("finds drafts on this computer when the project's versions can't be read", () => {
    const offline = find("notify-customer@1.0.0", { kind: "offline" });
    expect(offline.state === "found" && offline.source).toBe("local");
    expect(offline.contract?.callable).toBeNull();
    expect(find("customer-profile@2.0.0", { kind: "offline" })).toEqual({
      state: "unavailable",
      contract: null,
      message: "Connect to a platform to call published workflows.",
    });
    expect(find("customer-profile@2.0.0", { kind: "forbidden" })).toEqual({
      state: "unavailable",
      contract: null,
      message: "You can't read this project's published workflows here.",
    });
  });

  it("waits for the project's versions before saying a version is missing", () => {
    expect(find("customer-profile@2.0.0", { kind: "loading" })).toEqual({
      state: "loading",
      contract: null,
    });
  });

  it("asks for a workflow when the step names none", () => {
    for (const uses of ["", "notify-customer", "@1.0.0", "notify-customer@"])
      expect(find(uses)).toEqual({
        state: "unset",
        contract: null,
        message: "Choose a workflow to call.",
      });
  });
});
