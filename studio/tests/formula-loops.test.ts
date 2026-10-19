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
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import {
  LoopScopes,
  enclosingLoops,
  stepPointerOf,
} from "../src/app/editor/formula/loops";

const { cases } = JSON.parse(
  readFileSync(
    resolve(import.meta.dirname, "fixtures/language/scope.json"),
    "utf8",
  ),
) as {
  cases: {
    name: string;
    document: unknown;
    path: string;
    result: { entries: unknown[] };
  }[];
};
const nested = cases.find((item) =>
  item.name.startsWith("an inner loop reads"),
)!;

describe("loop bodies", () => {
  it("names the loops around a step, innermost first", () => {
    expect(enclosingLoops(nested.document, "pair")).toEqual(["line", "batch"]);
    expect(enclosingLoops(nested.document, "batch")).toEqual([]);
    expect(enclosingLoops(nested.document, "absent")).toEqual([]);
  });
  it("finds where a step sits in the document", () => {
    expect(stepPointerOf(nested.document, "pair")).toBe(
      "/spec/steps/0/body/steps/0/body/steps/0",
    );
    expect(stepPointerOf(nested.document, "absent")).toBeNull();
  });
  it("asks the Studio host once per document and field, and keeps the answer", async () => {
    const asked: unknown[] = [];
    let changed = 0;
    const scopes = new LoopScopes(
      async (body) => {
        asked.push(body);
        return nested.result;
      },
      () => changed++,
    );
    expect(scopes.entries(nested.document, nested.path)).toBeNull();
    expect(scopes.entries(nested.document, nested.path)).toBeNull();
    await new Promise((settle) => setTimeout(settle, 0));
    expect(asked).toEqual([
      {
        source: JSON.stringify(nested.document),
        format: "json",
        path: nested.path,
      },
    ]);
    expect(changed).toBe(1);
    expect(
      scopes.entries(nested.document, nested.path)?.map((entry) => entry.ref),
    ).toContain("/loops/batch/index");
  });
  it("answers nothing, quietly, when the host can't", async () => {
    const scopes = new LoopScopes(
      async () => {
        throw new Error("offline");
      },
      () => undefined,
    );
    scopes.entries(nested.document, nested.path);
    await new Promise((settle) => setTimeout(settle, 0));
    expect(scopes.entries(nested.document, nested.path)).toEqual([]);
  });
});
