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
import { describe, expect, it } from "vitest";
import type { HttpActionBuildResult } from "../src/app/integrations/http-action-client";
import { ConnectorActions } from "../src/app/editor/ndv/owned/connector-actions";
import { OwnedDocuments } from "../src/app/editor/ndv/owned/owned-documents";
import {
  DEFAULT_RETRY,
  newConnectorRecipe,
  newHttpRecipe,
} from "../src/app/editor/ndv/owned/owned-actions";
import { withOwnedAction } from "../src/app/editor/ndv/owned/owned-store";
import { StructuredCanvasAdapter } from "../src/app/model";

const USES = "untitled-workflow.get@1.0.0";

function fixture(
  get: (path: string) => Promise<unknown> = async () => ({ actions: [] }),
) {
  const model = new StructuredCanvasAdapter();
  model.insert("action", "root", undefined, {
    id: "ignored",
    uses: USES,
    with: { object: { query: { literal: { limit: "25" } } } },
  });
  model.canvas = withOwnedAction(model.canvas, USES, {
    ...newHttpRecipe(),
    pathTemplate: "/orders",
    retry: DEFAULT_RETRY,
  });
  const cached = new Map<string, Record<string, unknown> | null>();
  const requests: Record<string, unknown>[] = [];
  const answers: {
    done: (result: HttpActionBuildResult) => void;
    fail: (error: unknown) => void;
  }[] = [];
  let refreshes = 0;
  let cacheCalls = 0;
  const host = {
    model,
    // Like the shell: a document is kept, null drops it.
    cacheContract: (uses: string, document: Record<string, unknown> | null) => {
      cacheCalls++;
      if (document) cached.set(uses, document);
      else cached.delete(uses);
    },
    refreshView: () => {
      refreshes++;
    },
  };
  const documents = new OwnedDocuments(
    host,
    (request) => {
      requests.push(request);
      return new Promise((done, fail) => answers.push({ done, fail }));
    },
    new ConnectorActions(get),
  );
  /** Answers request `index` (the latest by default). */
  const reply = (result: HttpActionBuildResult, index = answers.length - 1) =>
    answers[index].done(result);
  /** Makes request `index` (the latest by default) fail. */
  const fail = (error: unknown, index = answers.length - 1) =>
    answers[index].fail(error);
  return {
    model,
    cached,
    requests,
    documents,
    reply,
    fail,
    refreshes: () => refreshes,
    cacheCalls: () => cacheCalls,
  };
}
const built = (method = "GET"): HttpActionBuildResult => ({
  ok: true,
  compiled: true,
  diagnostics: [],
  action: {
    kind: "Action",
    spec: { sideEffect: method === "GET" ? "read_only" : "non_idempotent" },
  },
});
const nextTick = () => new Promise((done) => setTimeout(done, 0));

describe("owned action documents", () => {
  it("builds a changed recipe once and caches the document with its retry", async () => {
    const { documents, requests, reply, cached } = fixture();
    documents.sync();
    documents.sync();
    expect(requests).toHaveLength(1);
    expect(requests[0]).toMatchObject({
      pathTemplate: "/orders",
      parameters: [{ name: "limit", location: "query" }],
    });
    reply(built());
    await documents.settled();
    expect(cached.get(USES)).toEqual({
      kind: "Action",
      spec: { sideEffect: "read_only", retry: DEFAULT_RETRY },
    });
  });
  it("keeps the builder's problems for the step's fields", async () => {
    const { documents, reply } = fixture();
    documents.sync();
    reply({
      ok: false,
      compiled: false,
      action: null,
      diagnostics: [
        {
          code: "WV-HTTP-ACTION-PATH",
          severity: "error",
          message: "Bad path.",
          path: "/pathTemplate",
        },
      ],
    });
    await documents.settled();
    expect(documents.problems().get(USES)?.[0].path).toBe("/pathTemplate");
  });
  it("ignores an answer for a recipe that changed since", async () => {
    const { documents, model, reply, cached, requests } = fixture();
    documents.sync();
    model.canvas = withOwnedAction(model.canvas, USES, {
      ...newHttpRecipe(),
      pathTemplate: "/orders/{id}",
    });
    documents.sync();
    expect(requests).toHaveLength(2);
    reply(built("POST"), 0);
    await nextTick();
    expect(cached.has(USES)).toBe(false);
    reply(built(), 1);
    await documents.settled();
    expect(cached.get(USES)).toEqual({
      kind: "Action",
      spec: { sideEffect: "read_only" },
    });
  });
  it("builds from the step wherever it sits, in a decision case too", async () => {
    const { documents, model, requests, reply } = fixture();
    // The root step calls another action, so the only step that calls ours sits inside the decision.
    model.update(
      "ignored",
      JSON.stringify({ id: "ignored", kind: "action", uses: "other@1.0.0" }),
    );
    const decision = model.insert("switch");
    model.insert("action", `${decision.id}/case 1`, undefined, {
      id: "nested",
      uses: USES,
      with: { object: { headers: { literal: { "X-Trace": "t" } } } },
    });
    documents.sync();
    expect(requests).toHaveLength(1);
    expect(requests[0]).toMatchObject({
      parameters: [{ name: "X-Trace", location: "header" }],
    });
    reply(built());
    await documents.settled();
  });
  it("clears the problems of a recipe that can't be built yet", async () => {
    const { documents, model, reply, requests } = fixture();
    documents.sync();
    reply({
      ok: false,
      compiled: false,
      action: null,
      diagnostics: [
        { code: "X", severity: "error", message: "Bad.", path: "/method" },
      ],
    });
    await documents.settled();
    expect(documents.problems().get(USES)).toHaveLength(1);
    model.canvas = withOwnedAction(model.canvas, USES, {
      ...newHttpRecipe(),
      pathTemplate: "",
    });
    documents.sync();
    expect(requests).toHaveLength(1);
    expect(documents.problems().has(USES)).toBe(false);
  });
  it("forgets an action the workflow no longer owns, and drops its late answer", async () => {
    const { documents, model, reply, cached, requests } = fixture();
    documents.sync();
    model.canvas = withOwnedAction(model.canvas, USES, null);
    documents.sync();
    reply(built());
    await nextTick();
    expect(cached.has(USES)).toBe(false);
    model.canvas = withOwnedAction(model.canvas, USES, {
      ...newHttpRecipe(),
      pathTemplate: "/orders",
      retry: DEFAULT_RETRY,
    });
    documents.sync();
    expect(requests).toHaveLength(2);
    reply(built());
    await documents.settled();
    expect(cached.has(USES)).toBe(true);
  });
});

describe("the cached document of an owned action that goes away", () => {
  /** Builds and caches the document of the fixture's action. */
  async function cacheIt(f: ReturnType<typeof fixture>) {
    f.documents.sync();
    f.reply(built());
    await f.documents.settled();
    expect(f.cached.has(USES)).toBe(true);
  }
  const recipeAt = (pathTemplate: string) => ({
    ...newHttpRecipe(),
    pathTemplate,
  });

  it("drops the document when the workflow stops owning the action", async () => {
    const f = fixture();
    await cacheIt(f);
    const refreshed = f.refreshes();
    f.model.canvas = withOwnedAction(f.model.canvas, USES, null);
    f.documents.sync();
    expect(f.cached.has(USES)).toBe(false);
    expect(f.refreshes()).toBe(refreshed + 1);
  });
  it("drops the document when the path is emptied, without asking the host", async () => {
    const f = fixture();
    await cacheIt(f);
    f.model.canvas = withOwnedAction(f.model.canvas, USES, recipeAt(""));
    f.documents.sync();
    expect(f.cached.has(USES)).toBe(false);
    expect(f.requests).toHaveLength(1);
  });
  it("doesn't hand a deleted action's document to a new action of the same name", async () => {
    const f = fixture();
    await cacheIt(f);
    // Deleted, then added again with the same name and no path yet.
    f.model.canvas = withOwnedAction(f.model.canvas, USES, null);
    f.documents.sync();
    f.model.canvas = withOwnedAction(f.model.canvas, USES, recipeAt(""));
    f.documents.sync();
    expect(f.cached.has(USES)).toBe(false);
  });
  it("drops it even when the same name comes back before anyone looked", async () => {
    const f = fixture();
    await cacheIt(f);
    f.model.canvas = withOwnedAction(f.model.canvas, USES, null);
    f.model.canvas = withOwnedAction(f.model.canvas, USES, recipeAt(""));
    f.documents.sync();
    expect(f.cached.has(USES)).toBe(false);
  });
  it("drops the document of a connector action too", async () => {
    const SEND = "untitled-workflow.send-email@1.0.0";
    const f = fixture(async () => ({
      actions: [
        {
          connector: "weave-email@1.0.0",
          action: "send",
          document: { kind: "Action", metadata: {}, spec: {} },
        },
      ],
    }));
    f.model.canvas = withOwnedAction(f.model.canvas, USES, null);
    f.model.canvas = withOwnedAction(
      f.model.canvas,
      SEND,
      newConnectorRecipe("weave-email@1.0.0", "send"),
    );
    f.documents.sync();
    await f.documents.settled();
    expect(f.cached.has(SEND)).toBe(true);
    f.model.canvas = withOwnedAction(f.model.canvas, SEND, null);
    f.documents.sync();
    expect(f.cached.has(SEND)).toBe(false);
  });
  it("leaves the cache alone when nothing of the action was ever cached", () => {
    const f = fixture();
    f.model.canvas = withOwnedAction(f.model.canvas, USES, recipeAt(""));
    f.documents.sync();
    f.model.canvas = withOwnedAction(f.model.canvas, USES, null);
    f.documents.sync();
    expect(f.cacheCalls()).toBe(0);
    expect(f.refreshes()).toBe(0);
  });
});

describe("when the host can't build an owned action", () => {
  it("shows a problem at the URL instead of staying silent, and builds again on the next change", async () => {
    const { documents, fail, reply, requests, cached, refreshes } = fixture();
    documents.sync();
    fail(new Error("Studio host unreachable."));
    await documents.settled();
    const [problem] = documents.problems().get(USES) ?? [];
    expect(problem).toMatchObject({
      severity: "error",
      path: "/pathTemplate",
      message: "Couldn't build the API action. Check the URL and try again.",
    });
    expect(problem.code).toMatch(/^WV-/);
    expect(refreshes()).toBe(1);
    expect(cached.has(USES)).toBe(false);
    documents.sync();
    expect(requests).toHaveLength(2);
    reply(built());
    await documents.settled();
    expect(documents.problems().has(USES)).toBe(false);
    expect(cached.has(USES)).toBe(true);
  });
  it("shows a problem when the host refuses without saying why", async () => {
    const { documents, reply, cached } = fixture();
    documents.sync();
    reply({ ok: false, compiled: false, action: null, diagnostics: [] });
    await documents.settled();
    expect(documents.problems().get(USES)).toMatchObject([
      {
        severity: "error",
        path: "/pathTemplate",
        message: "Couldn't build the API action. Check the URL and try again.",
      },
    ]);
    expect(cached.has(USES)).toBe(false);
  });
  it("doesn't blame a recipe that changed since the failed call", async () => {
    const { documents, model, fail, reply } = fixture();
    documents.sync();
    model.canvas = withOwnedAction(model.canvas, USES, {
      ...newHttpRecipe(),
      pathTemplate: "/orders/{id}",
    });
    documents.sync();
    fail(new Error("late"), 0);
    await nextTick();
    expect(documents.problems().has(USES)).toBe(false);
    reply(built(), 1);
    await documents.settled();
    expect(documents.problems().has(USES)).toBe(false);
  });
});

describe("connector action documents", () => {
  const template = {
    connector: "weave-email@1.0.0",
    action: "send",
    document: {
      apiVersion: "weave/v1alpha1",
      kind: "Action",
      metadata: { name: "weave-email-send", version: "1.0.0" },
      spec: {
        sideEffect: "non_idempotent",
        timeoutSeconds: 60,
        inputSchema: { type: "object" },
      },
    },
  };
  const SEND = "untitled-workflow.send-email@1.0.0";
  function withEmailStep(get: (path: string) => Promise<unknown>) {
    const f = fixture(get);
    f.model.canvas = withOwnedAction(f.model.canvas, USES, null);
    f.model.insert("action", "root", undefined, {
      id: "send-email",
      uses: SEND,
    });
    f.model.canvas = withOwnedAction(f.model.canvas, SEND, {
      ...newConnectorRecipe("weave-email@1.0.0", "send"),
      timeoutSeconds: 90,
    });
    return f;
  }

  it("copies the built-in action as the workflow's own document", async () => {
    const asked: string[] = [];
    const { documents, cached, requests, refreshes } = withEmailStep(
      async (path) => {
        asked.push(path);
        return { actions: [template] };
      },
    );
    documents.sync();
    documents.sync();
    await documents.settled();
    expect(asked).toEqual(["/studio/contracts/connector-actions"]);
    expect(requests).toHaveLength(0);
    expect(cached.get(SEND)).toMatchObject({
      metadata: { name: "untitled-workflow.send-email", version: "1.0.0" },
      spec: { sideEffect: "non_idempotent", timeoutSeconds: 90 },
    });
    expect(refreshes()).toBe(1);
  });
  it("shows a problem when the list of built-in actions can't be loaded, and tries again on the next change", async () => {
    let up = false;
    const { documents, cached } = withEmailStep(async () => {
      if (!up) throw new Error("Studio host unreachable.");
      return { actions: [template] };
    });
    documents.sync();
    await documents.settled();
    expect(documents.problems().get(SEND)).toMatchObject([
      {
        severity: "error",
        message:
          "Couldn't load the action this step uses. Try again in a moment.",
      },
    ]);
    expect(cached.has(SEND)).toBe(false);
    up = true;
    documents.sync();
    await documents.settled();
    expect(documents.problems().has(SEND)).toBe(false);
    expect(cached.has(SEND)).toBe(true);
  });
  it("shows a problem when this Studio has no such built-in action", async () => {
    const { documents, cached } = withEmailStep(async () => ({ actions: [] }));
    documents.sync();
    await documents.settled();
    expect(documents.problems().get(SEND)).toMatchObject([
      {
        severity: "error",
        message: "Studio doesn't have the action this step uses.",
      },
    ]);
    expect(cached.has(SEND)).toBe(false);
  });
});

describe("the built-in connector actions", () => {
  const entry = {
    connector: "weave-email@1.0.0",
    action: "send",
    document: { kind: "Action" },
  };
  it("loads the list once and finds an action by connector and name", async () => {
    let calls = 0;
    const actions = new ConnectorActions(async () => {
      calls++;
      return { actions: [entry, { ...entry, action: "reply" }] };
    });
    const [first, second] = await Promise.all([actions.list(), actions.list()]);
    expect(first).toBe(second);
    expect(calls).toBe(1);
    expect((await actions.find("weave-email@1.0.0", "reply"))?.action).toBe(
      "reply",
    );
    expect(await actions.find("weave-email@1.0.0", "forward")).toBeNull();
    expect(await actions.find("weave-sftp@1.0.0", "send")).toBeNull();
    expect(calls).toBe(1);
  });
  it("asks again after a failure instead of remembering it", async () => {
    let calls = 0;
    const actions = new ConnectorActions(async () => {
      if (++calls === 1) throw new Error("offline");
      return { actions: [entry] };
    });
    await expect(actions.list()).rejects.toThrow("offline");
    expect(await actions.list()).toEqual([entry]);
    expect(calls).toBe(2);
  });
  it("refuses an answer it can't read, and asks again later", async () => {
    for (const bad of [
      null,
      "text",
      {},
      { actions: "none" },
      { actions: [{ connector: 3 }] },
    ]) {
      let calls = 0;
      const actions = new ConnectorActions(async () => {
        calls++;
        return calls === 1 ? bad : { actions: [entry] };
      });
      await expect(actions.list()).rejects.toThrow(
        "Studio sent a list of built-in actions it can't read.",
      );
      expect(await actions.list()).toEqual([entry]);
    }
  });
});
