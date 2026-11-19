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
// The workflows an execution reaches: the ones it calls, and through the
// drafts on this computer among them, the ones those call; each draft here
// goes with its source so the host runs it.
import { describe, expect, it } from "vitest";
import { stringify } from "yaml";
import { calledWorkflows } from "../src/app/editor/calls/callee-drafts";
import { LocalDrafts, type DraftStorage } from "../src/app/local-drafts";

function memoryStorage(): DraftStorage {
  const items = new Map<string, string>();
  return {
    getItem: (key) => items.get(key) ?? null,
    setItem: (key, value) => void items.set(key, value),
    removeItem: (key) => void items.delete(key),
  };
}
const calling = (name: string, version: string, ...uses: string[]) => ({
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name, version },
  spec: {
    steps: uses.map((reference, n) => ({
      id: `call-${n}`,
      kind: "callWorkflow",
      uses: reference,
      mode: "wait",
      with: { literal: {} },
    })),
    output: { literal: {} },
  },
});
function drafts(...documents: { doc: object; format: "yaml" | "json" }[]) {
  const store = new LocalDrafts(() => storage);
  const storage = memoryStorage();
  documents.forEach(({ doc, format }, n) => {
    const { name, version } = (
      doc as { metadata: { name: string; version: string } }
    ).metadata;
    store.save(
      { id: `d${n}`, name, version, savedAt: "2026-11-18T10:00:00Z" },
      {
        source: format === "json" ? JSON.stringify(doc) : stringify(doc),
        format,
        layout: null,
        savedAt: "2026-11-18T10:00:00Z",
      },
    );
  });
  return store;
}

describe("the workflows an execution reaches", () => {
  it("sends each draft here with its source, and follows the calls inside it", () => {
    const lookup = calling("lookup-customer", "0.1.0", "audit-trail@2.0.0");
    const store = drafts(
      { doc: lookup, format: "yaml" },
      { doc: calling("unrelated", "1.0.0"), format: "json" },
    );
    const reached = calledWorkflows(
      store,
      calling(
        "order-intake",
        "1.2.0",
        "lookup-customer@0.1.0",
        "notify-customer@1.0.0",
        "lookup-customer@0.1.0",
      ),
    );
    expect(reached.references).toEqual([
      "lookup-customer@0.1.0",
      "notify-customer@1.0.0",
      "audit-trail@2.0.0",
    ]);
    expect(reached.sources).toEqual({
      "lookup-customer@0.1.0": { source: stringify(lookup), format: "yaml" },
    });
  });

  it("stops at a draft that calls back, and reads no draft without storage", () => {
    const store = drafts({
      doc: calling("ping", "1.0.0", "order-intake@1.2.0"),
      format: "json",
    });
    const reached = calledWorkflows(
      store,
      calling("order-intake", "1.2.0", "ping@1.0.0"),
    );
    expect(reached.references).toEqual(["ping@1.0.0", "order-intake@1.2.0"]);
    expect(Object.keys(reached.sources)).toEqual(["ping@1.0.0"]);
    expect(
      calledWorkflows(undefined, calling("x", "1.0.0", "ping@1.0.0")),
    ).toEqual({ sources: {}, references: ["ping@1.0.0"] });
  });

  it("leaves a draft out when the project publishes the same version", () => {
    const store = drafts({
      doc: calling("lookup-customer", "0.1.0", "audit-trail@2.0.0"),
      format: "yaml",
    });
    const reached = calledWorkflows(
      store,
      calling("order-intake", "1.2.0", "lookup-customer@0.1.0"),
      (reference) => reference === "lookup-customer@0.1.0",
    );
    expect(reached).toEqual({
      sources: {},
      references: ["lookup-customer@0.1.0"],
    });
  });
});
