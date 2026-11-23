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
// A platform that predates simulated failures refuses a test data document
// that holds them, naming no path. Studio decides it can't save them only
// after the same save without them succeeds: every other section is saved,
// and the failures stay for this session. Any other refusal is as before.
import "@angular/compiler";
import { afterAll, describe, expect, it, vi } from "vitest";
vi.hoisted(() =>
  vi.stubGlobal("document", {
    addEventListener: vi.fn(),
    documentElement: { style: {} },
  }),
);
import { App } from "../src/app/app";
import { Autosave } from "../src/app/editor/state/autosave";
import {
  emptyTestData,
  withFailure,
  withPin,
} from "../src/app/editor/state/test-data-ops";
import { TestDataService } from "../src/app/editor/state/test-data-service";
afterAll(() => vi.unstubAllGlobals());

const AT = "2026-11-20T10:00:00Z";
const failing = withFailure(
  withPin(
    emptyTestData(AT),
    "lookup",
    {
      output: { id: 1 },
      source: "manual",
      pinnedAt: AT,
      configHash: "fnv1a64:0000000000000001",
      contractHash: "fnv1a64:0000000000000002",
      upstreamHash: "fnv1a64:0000000000000003",
    },
    AT,
  ),
  "submit",
  { code: "REQUEST_REFUSED", source: "manual" },
  AT,
);

/** An App with what saving test data reads, and a platform refusing as `refuse` says. */
function fixture(refuse: (document: Record<string, unknown>) => object | null) {
  const app = Object.create(App.prototype) as App;
  const sent: Record<string, unknown>[] = [];
  const api = {
    project: "/p",
    mutate: async (
      _path: string,
      _method: string,
      body: { document: Record<string, unknown> },
    ) => {
      sent.push(structuredClone(body.document));
      const refusal = refuse(body.document);
      if (refusal) throw refusal;
      return {
        draft_id: "A",
        kind: "test-data",
        revision: sent.length,
        draft_revision: 3,
        document: body.document,
        contains_run_data: false,
      };
    },
  };
  const testData = new TestDataService({
    api: { request: async () => ({ entries: [] }) } as never,
    backend: {
      keys: async () => [],
      get: async () => null,
      set: async () => true,
      remove: async () => true,
    },
    now: () => new Date(AT),
    inputs: () => ({
      source: "{}",
      definitions: [],
      schemaOf: () => undefined,
    }),
  });
  const toasts: string[] = [];
  Object.assign(app, {
    api,
    cdr: { markForCheck() {} },
    draftId: "A",
    draftRevision: 3,
    testData,
    tools: null,
    platformDocs: { draftId: "A", testData: null, canvas: null },
    autosave: new Autosave(async () => true),
    notify(text: string) {
      toasts.push(text);
    },
  });
  const save = (document: object) =>
    (
      app as unknown as {
        savePlatformDocument(kind: string, document: object): Promise<boolean>;
      }
    ).savePlatformDocument("test-data", document);
  return { app, sent, testData, toasts, save };
}
const schemaRefusal = {
  status: 422,
  detail: { code: "WV-STUDIO-DOCUMENT-SCHEMA" },
};

describe("simulated failures on a platform that can't save them", () => {
  it("saves everything else, and keeps the failures for this session", async () => {
    const { sent, testData, save } = fixture((document) =>
      "errors" in document ? schemaRefusal : null,
    );
    await testData.change(failing, []);
    expect(await save(testData.toPlatform().document)).toBe(true);
    expect(sent).toHaveLength(2);
    expect(sent[1]).not.toHaveProperty("errors");
    expect(sent[1]["pins"]).toEqual(failing.pins);
    expect(testData.failuresInSession()).toBe(true);
    expect(testData.data().errors).toEqual(failing.errors);
  });

  it("keeps today's answer when the platform refuses the document without them too", async () => {
    const { sent, testData, save } = fixture(() => schemaRefusal);
    await testData.change(failing, []);
    expect(await save(testData.toPlatform().document)).toBe(false);
    expect(sent).toHaveLength(2);
    expect(testData.failuresInSession()).toBe(false);
  });

  it("tries nothing more for another refusal, or a document without failures", async () => {
    const other = fixture(() => ({
      status: 422,
      detail: { code: "WV-STUDIO-DOCUMENT-INVALID" },
    }));
    await other.testData.change(failing, []);
    expect(await other.save(other.testData.toPlatform().document)).toBe(false);
    expect(other.sent).toHaveLength(1);
    const plain = fixture(() => schemaRefusal);
    expect(await plain.save(emptyTestData(AT))).toBe(false);
    expect(plain.sent).toHaveLength(1);
    expect(plain.testData.failuresInSession()).toBe(false);
  });

  it("tries nothing more for a schema refusal that names its paths: a value is wrong, not the section", async () => {
    const named = fixture(() => ({
      status: 422,
      detail: {
        code: "WV-STUDIO-DOCUMENT-SCHEMA",
        result: { paths: ["/pins/lookup/output"] },
      },
    }));
    await named.testData.change(failing, []);
    expect(await named.save(named.testData.toPlatform().document)).toBe(false);
    expect(named.sent).toHaveLength(1);
    expect(named.testData.failuresInSession()).toBe(false);
  });
});
