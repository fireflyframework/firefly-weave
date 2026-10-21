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
import {
  TestDataService,
  type TestDataHost,
} from "../src/app/editor/state/test-data-service";
import {
  emptyTestData,
  entriesOf,
  withInput,
  withPin,
} from "../src/app/editor/state/test-data-ops";
import { saveStudioDocument } from "../src/app/editor/state/studio-documents";
import type { StoreBackend } from "../src/app/editor/state/browser-store";
import type {
  PinEntry,
  StudioDocument,
} from "../src/app/editor/state/test-data";

const NOW = new Date("2026-10-20T10:00:00Z");
const ISO = NOW.toISOString();
const SCOPE = {
  profileName: "Acme",
  subject: "user-1",
  tenantId: "7b0c1c43-3f5a-4cde-9f0e-3c2d5a6b7c8d",
  projectId: "9b1c2d3e-4f50-4a61-8b72-c3d4e5f60718",
};
const pin = (output: unknown): PinEntry => ({
  output: output as PinEntry["output"],
  source: "manual",
  pinnedAt: ISO,
  configHash: "fnv1a64:0000000000000001",
  contractHash: "fnv1a64:0000000000000002",
  upstreamHash: "fnv1a64:0000000000000003",
});
function host() {
  const values = new Map<string, string>();
  const backend: StoreBackend = {
    keys: async (prefix) =>
      [...values.keys()].filter((key) => key.startsWith(prefix)),
    get: async (key) => values.get(key) ?? null,
    set: async (key, value) => (values.set(key, value), true),
    remove: async (key) => values.delete(key),
  };
  const value: TestDataHost = {
    api: {
      request: async <T>(_path: string, _method?: string, body?: unknown) => {
        const data = (body as { testData: Record<string, unknown> }).testData;
        const paths = [
          ...(data["input"] ? ["/input"] : []),
          ...Object.keys((data["pins"] as object) ?? {}).map(
            (key) => `/pins/${key}`,
          ),
        ];
        return { entries: paths.map((path) => ({ path, ok: true })) } as T;
      },
    },
    backend,
    now: () => NOW,
    inputs: () => ({
      source: "{}",
      definitions: [],
      schemaOf: () => ({ type: "object" }) as never,
    }),
  };
  return { value, values };
}
const platformDocument = (document: object, revision = 3): StudioDocument =>
  ({
    draft_id: "d-1",
    kind: "test-data",
    revision,
    draft_revision: 2,
    document,
    contains_run_data: false,
    source_environment_ids: [],
    updated_at: ISO,
    updated_by: "someone",
  }) as StudioDocument;

describe("test data on the platform", () => {
  it("a platform document replaces the local copy but keeps entries waiting for publishing", async () => {
    const { value } = host();
    const service = new TestDataService(value);
    await service.open({ kind: "connected", scope: SCOPE, draftId: "d-1" });
    let local = withInput(emptyTestData(ISO), { mine: true }, "manual", ISO);
    local = withPin(local, "lookup", pin({ id: 1 }), ISO);
    expect((await service.change(local, ["/input", "/pins/lookup"])).ok).toBe(
      true,
    );
    service.markLocalOnly(["/pins/lookup"]);
    const theirs = withInput(
      emptyTestData(ISO),
      { theirs: true },
      "manual",
      ISO,
    );
    service.syncFrom(platformDocument(theirs));
    expect(service.data().input?.value).toEqual({ theirs: true });
    expect(service.data().pins?.["lookup"]?.output).toEqual({ id: 1 });
    expect(service.revision()).toBe(3);
  });
  it("the platform never receives an entry kept on this computer", async () => {
    const { value } = host();
    const service = new TestDataService(value);
    await service.open({ kind: "connected", scope: SCOPE, draftId: "d-1" });
    let local = withInput(emptyTestData(ISO), { a: 1 }, "manual", ISO);
    local = withPin(local, "lookup", pin({ id: 1 }), ISO);
    await service.change(local, ["/input", "/pins/lookup"]);
    service.markLocalOnly(["/pins/lookup"]);
    const { document, localOnly } = service.toPlatform();
    expect(localOnly).toEqual(["/pins/lookup"]);
    expect(entriesOf(document).map((entry) => entry.path)).toEqual(["/input"]);
    // The list survives reopening.
    await service.flush();
    const reopened = new TestDataService(value);
    await reopened.open({ kind: "connected", scope: SCOPE, draftId: "d-1" });
    expect([...reopened.localOnly()]).toEqual(["/pins/lookup"]);
  });
  it("a value the platform refuses as secret leaves every copy on this computer", async () => {
    const { value, values } = host();
    const service = new TestDataService(value);
    await service.open({ kind: "connected", scope: SCOPE, draftId: "d-1" });
    const local = withPin(
      emptyTestData(ISO),
      "token",
      pin({ token: "s3cret-canary" }),
      ISO,
    );
    await service.change(local, ["/pins/token"]);
    await service.flush();
    expect([...values.values()].join("")).toContain("s3cret-canary");
    await service.dropSecret(["/pins/token"]);
    expect(JSON.stringify(service.data())).not.toContain("s3cret-canary");
    expect([...values.values()].join("")).not.toContain("s3cret-canary");
  });
  it("a changed document on the platform asks before replacing either side", async () => {
    const stored = { revision: 4 };
    const api = {
      project: "/studio/api/api/v1/tenants/t/projects/p",
      mutate: async <T>(
        _path: string,
        _method: string,
        _body: unknown,
        revision?: number,
      ): Promise<T> => {
        if (revision !== stored.revision)
          throw Object.assign(new Error("Stale"), {
            status: 412,
            detail: { status: 412, code: "WV-ETAG", message: "Stale" },
          });
        return platformDocument({}, revision + 1) as T;
      },
    };
    const body = { draft_revision: 2, document: emptyTestData(ISO) as never };
    expect(await saveStudioDocument(api, "d-1", "test-data", body, 3)).toEqual({
      ok: false,
      status: 412,
      code: "WV-ETAG",
      paths: [],
    });
    const saved = await saveStudioDocument(api, "d-1", "test-data", body, 4);
    expect(saved.ok && saved.document.revision).toBe(5);
  });
});
