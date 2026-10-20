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
// The draft autosave's guards, one by one: it sends nothing while the
// compare flow is open for the draft, never sets or clears the shared busy
// command, sends the content read when it started, and a test-data or canvas
// save answered after a switch changes nothing of the draft open now.
import "@angular/compiler";
import { afterAll, describe, expect, it, vi } from "vitest";
vi.hoisted(() =>
  vi.stubGlobal("document", {
    addEventListener: vi.fn(),
    documentElement: { style: {} },
  }),
);
import { App } from "../src/app/app";
import { StructuredCanvasAdapter } from "../src/app/model";
import { Autosave } from "../src/app/editor/state/autosave";
afterAll(() => vi.unstubAllGlobals());

interface Sent {
  path: string;
  body: unknown;
  revision: number | undefined;
}
/** An App with only what autosaving a platform draft reads, and a platform it controls. */
function fixture() {
  const app = Object.create(App.prototype) as App;
  const model = new StructuredCanvasAdapter();
  model.insert("wait");
  const sent: Sent[] = [];
  let answer: ((value: Record<string, unknown>) => void) | null = null;
  let hold = false;
  const api = {
    project: "/p",
    session: { profile: { name: "Acme" } },
    mutate: (
      path: string,
      _method: string,
      body: unknown,
      revision?: number,
    ) => {
      sent.push({ path, body: structuredClone(body), revision });
      if (!hold)
        return Promise.resolve({ id: "A", revision: (revision ?? 0) + 1 });
      return new Promise((done) => (answer = done));
    },
  };
  Object.assign(app, {
    api,
    model,
    sourceBuffer: model.source,
    cdr: { markForCheck() {} },
    dirty: true,
    draftId: "A",
    draftRevision: 3,
    pendingMutation: null,
    busy: "",
    conflict: null,
    saveForced: false,
    flushInspector() {},
    forgetRecovery() {},
    flushDraftCanvas: async () => "platform",
    flushPlatformDocuments: async () => true,
  });
  Object.defineProperty(app, "inspectorProblem", { get: () => "" });
  const autosave = new Autosave((generation) =>
    (
      app as unknown as { autosaveDraft(g: number): Promise<boolean> }
    ).autosaveDraft(generation),
  );
  Object.assign(app, { autosave });
  return {
    app: app as unknown as App & Record<string, unknown>,
    model,
    sent,
    autosave,
    hold: () => (hold = true),
    answer: (value: Record<string, unknown>) => answer?.(value),
  };
}

describe("a platform draft's autosave", () => {
  it("sends nothing while the compare flow is open for the draft", async () => {
    const { app, sent, autosave } = fixture();
    Object.assign(app, { conflict: { draftId: "A" } });
    autosave.touch();
    expect(await autosave.flush()).toBe(false);
    expect(sent).toEqual([]);
  });
  it("never sets or clears the busy command another action set", async () => {
    const { app, sent, autosave, hold, answer } = fixture();
    Object.assign(app, { busy: "Open draft" });
    hold();
    autosave.touch();
    const saving = autosave.flush();
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    expect(app["busy"]).toBe("Open draft");
    answer({ id: "A", revision: 4 });
    await saving;
    expect(app["busy"]).toBe("Open draft");
  });
  it("sends the content and revision it read when it started, after resending an unanswered save", async () => {
    const { app, model, sent, autosave, hold, answer } = fixture();
    const read = structuredClone(model.definition);
    Object.assign(app, {
      pendingMutation: {
        path: "/p/drafts/A",
        method: "PUT",
        body: { document: read },
        revision: 3,
        key: "k-1",
        label: "Save draft",
      },
    });
    hold();
    autosave.touch();
    const saving = autosave.flush();
    // The resend is unanswered; meanwhile the workflow and its revision change.
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    model.insert("wait");
    Object.assign(app, { draftRevision: 9 });
    answer({ id: "A", revision: 4 });
    await vi.waitFor(() => expect(sent).toHaveLength(2));
    answer({ id: "A", revision: 5 });
    await saving;
    expect(sent[1].body).toEqual({ document: read });
    expect(sent[1].revision).toBe(4);
  });
  it("keeps a canvas save answered after a switch out of the draft open now", async () => {
    const { app, autosave, hold, answer } = fixture();
    Object.assign(app, {
      platformDocs: { draftId: "A", testData: null, canvas: null },
    });
    hold();
    const saving = (
      app as unknown as {
        savePlatformDocument(kind: string, document: object): Promise<boolean>;
      }
    ).savePlatformDocument("canvas", { schemaVersion: 2 });
    // Draft B opens while A's canvas save is unanswered.
    autosave.cancel();
    Object.assign(app, {
      draftId: "B",
      draftRevision: 1,
      platformDocs: { draftId: "B", testData: null, canvas: null },
    });
    answer({ draft_id: "A", kind: "canvas", revision: 7, document: {} });
    await saving;
    expect(app["platformDocs"]).toEqual({
      draftId: "B",
      testData: null,
      canvas: null,
    });
  });
});
