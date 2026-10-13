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
import { buildCatalog } from "../src/app/editor/add-step/entries";
import {
  GONE,
  insertEntry,
  placeMark,
  type InsertHost,
  type InsertRequest,
} from "../src/app/editor/add-step/insert";
import type {
  Insertion,
  TriggerKind,
} from "../src/app/editor/canvas/layout-ltr";
import { StructuredCanvasAdapter } from "../src/app/model";
import {
  catalogInput,
  entryById,
  installed,
  published,
} from "./add-step-fixtures";

type FakeHost = InsertHost & { calls: string[]; editingLocked: boolean };
function fakeHost(model = new StructuredCanvasAdapter()): FakeHost {
  const calls: string[] = [];
  return {
    calls,
    model,
    editingLocked: false,
    ensureApplied: async () => true,
    edit(change: () => void) {
      try {
        change();
        return "";
      } catch (error) {
        return error instanceof Error ? error.message : String(error);
      }
    },
    notify: (text: string) => void calls.push(`notify ${text}`),
    openInserted: (id: string) => void calls.push(`open ${id}`),
    openTrigger: (id: string, kind: TriggerKind) =>
      void calls.push(`trigger ${kind} ${id}`),
    insertCommon: async (
      common: string,
      at: Insertion,
      connector: string | undefined,
    ) => {
      calls.push(
        `common ${common} ${connector ?? "-"} ${at.owner}/${at.index}`,
      );
      return `${common}-x`;
    },
    insertPublished: async (
      uses: string,
      id: string | undefined,
      at: Insertion,
    ) => {
      calls.push(`action ${uses} ${id ?? ""} ${at.owner}/${at.index}`);
      return "call-action-1";
    },
    openTemplates: () => void calls.push("templates"),
    openNewApiAction: () => void calls.push("new api action"),
    loadActionCatalog: async (append = false, force = false) =>
      void calls.push(`load ${append} ${force}`),
  };
}
const catalog = buildCatalog(
  catalogInput({
    connectors: installed,
    actions: published,
    catalog: "ready",
    language: {
      features: ["text.concat", "flow.callWorkflow"],
      pending: new Set(),
    },
    hasSteps: false,
  }),
);
const pick = (id: string) => entryById(catalog, id);
/** A request for a place as it is now, as the panel records it when it opens. */
const fresh = (
  host: FakeHost,
  at: Insertion,
  opened = host.model.opened,
): InsertRequest => ({ at, opened, mark: placeMark(host.model, at) });
const ids = (model: StructuredCanvasAdapter) =>
  model.definition.spec.steps.map((step) => step.id);

describe("choosing an entry", () => {
  it("inserts a step where the + stood, as one undo step, and opens it", async () => {
    const host = fakeHost();
    host.model.insert("transform");
    host.model.insert("transform");
    const outcome = await insertEntry(
      host,
      pick("kind:switch"),
      fresh(host, { owner: "root", index: 1 }),
    );
    expect(outcome).toBe("done");
    expect(ids(host.model)).toEqual([
      "transform-1",
      "decision-1",
      "transform-2",
    ]);
    expect(host.calls).toEqual(["open decision-1"]);
    host.model.undo();
    expect(ids(host.model)).toEqual(["transform-1", "transform-2"]);
  });

  it("inserts presets with their fields and readable IDs", async () => {
    const host = fakeHost();
    const at = { owner: "root", index: 0 };
    const opened = host.model.opened;
    await insertEntry(
      host,
      pick("preset:text-template"),
      fresh(host, at, opened),
    );
    await insertEntry(host, pick("preset:free-text"), fresh(host, at, opened));
    await insertEntry(
      host,
      pick("preset:approve-reject"),
      fresh(host, at, opened),
    );
    await insertEntry(
      host,
      pick("preset:text-template"),
      fresh(host, at, opened),
    );
    const [second, approval, ask, text] = host.model.definition.spec.steps;
    expect(text).toMatchObject({
      id: "text-template",
      kind: "transform",
      value: { op: { name: "concat", args: [{ literal: "" }] } },
    });
    expect(second.id).toBe("text-template-2");
    expect(ask).toMatchObject({
      id: "ask-person",
      kind: "humanTask",
      decisions: ["submit"],
      formSchema: { required: ["answer"] },
    });
    expect(approval).toMatchObject({
      id: "approval-1",
      decisions: ["approve", "reject"],
    });
  });

  it("adds a trigger as one undo step and hands it to the shell", async () => {
    const host = fakeHost();
    const request = fresh(host, { owner: "root", index: 0 });
    expect(await insertEntry(host, pick("trigger:webhook"), request)).toBe(
      "done",
    );
    const [first] = host.model.canvas.triggers ?? [];
    expect(first.kind).toBe("webhook");
    expect(host.calls).toEqual([`trigger webhook ${first.id}`]);
    await insertEntry(host, pick("trigger:schedule"), request);
    expect(host.model.canvas.triggers?.map((intent) => intent.kind)).toEqual([
      "webhook",
      "schedule",
    ]);
    host.model.undo();
    expect(host.model.canvas.triggers?.map((intent) => intent.kind)).toEqual([
      "webhook",
    ]);
  });

  it("makes the workflow callable with Called by a workflow, in one undo step", async () => {
    const host = fakeHost();
    await insertEntry(
      host,
      pick("trigger:called"),
      fresh(host, { owner: "root", index: 0 }),
    );
    expect(host.model.definition.spec["callable"]).toEqual({});
    host.model.undo();
    expect(host.model.definition.spec["callable"]).toBeUndefined();
    expect(host.model.canvas.triggers).toBeUndefined();
  });

  it("hands common steps and published actions to the shell with their place, then opens them", async () => {
    const host = fakeHost();
    host.model.insert("transform");
    const request = fresh(host, { owner: "root", index: 1 });
    await insertEntry(host, pick("common:weave-http:call-api"), request);
    await insertEntry(host, pick("common:weave-sftp:write-file"), request);
    await insertEntry(host, pick("action:sql.lookup@1.1.0"), request);
    await insertEntry(host, catalog.customCall, request);
    expect(host.calls).toEqual([
      "common call-api - root/1",
      "open call-api-x",
      "common write-file weave-sftp@1.0.0 root/1",
      "open write-file-x",
      "action sql.lookup@1.1.0 a2 root/1",
      "open call-action-1",
      "common call-api - root/1",
      "open call-api-x",
    ]);
  });

  it("runs the panel's commands; loading more keeps the panel open", async () => {
    const host = fakeHost();
    const request = fresh(host, { owner: "root", index: 0 });
    expect(await insertEntry(host, pick("command:templates"), request)).toBe(
      "done",
    );
    expect(
      await insertEntry(host, pick("command:new-api-action"), request),
    ).toBe("done");
    const more = buildCatalog(
      catalogInput({ catalog: "ready", moreActions: true }),
    );
    expect(
      await insertEntry(host, entryById(more, "command:load-more"), request),
    ).toBe("kept-open");
    expect(host.calls).toEqual([
      "templates",
      "new api action",
      "load true false",
    ]);
  });

  it("refuses when the place is gone or the workflow changed, and says so", async () => {
    const host = fakeHost();
    const decision = host.model.insert("switch");
    const inside = fresh(host, { owner: `${decision.id}/case 1`, index: 0 });
    const opened = host.model.opened;
    host.model.removeSteps([decision.id]);
    expect(await insertEntry(host, pick("kind:wait"), inside)).toBe("gone");
    expect(host.calls).toEqual([`notify ${GONE}`]);
    expect(
      await insertEntry(
        host,
        pick("kind:wait"),
        fresh(host, { owner: "root", index: 0 }, opened + 1),
      ),
    ).toBe("gone");
    host.editingLocked = true;
    expect(
      await insertEntry(
        host,
        pick("kind:wait"),
        fresh(host, { owner: "root", index: 0 }, opened),
      ),
    ).toBe("refused");
    expect(host.calls.at(-1)).toBe(
      "notify Editing is paused while the simulation runs.",
    );
    expect(ids(host.model)).toEqual([]);
  });

  it("refuses when Undo, Redo or another edit moves the place, though its index is still inside the sequence", async () => {
    const host = fakeHost();
    for (const _ of [1, 2, 3, 4]) host.model.insert("transform");
    host.model.insert("wait", "root", 1);
    // After transform-2: wait-1 sits before it.
    const after = fresh(host, { owner: "root", index: 3 });
    expect(after.mark).toEqual({ before: "transform-2", after: "transform-3" });
    host.model.undo();
    expect(await insertEntry(host, pick("kind:wait"), after)).toBe("gone");
    host.model.redo();
    const redone = fresh(host, { owner: "root", index: 1 });
    host.model.undo();
    expect(await insertEntry(host, pick("kind:wait"), redone)).toBe("gone");
    const end = fresh(host, { owner: "root", index: 4 });
    host.model.insert("transform", "root", 0);
    expect(await insertEntry(host, pick("kind:wait"), end)).toBe("gone");
    expect(host.calls).toEqual([
      `notify ${GONE}`,
      `notify ${GONE}`,
      `notify ${GONE}`,
    ]);
    expect(ids(host.model)).toEqual([
      "transform-5",
      "transform-1",
      "transform-2",
      "transform-3",
      "transform-4",
    ]);
  });

  it("refuses when the workflow changes while unapplied edits are settled", async () => {
    const host = fakeHost();
    host.model.insert("transform");
    host.model.insert("transform");
    const request = fresh(host, { owner: "root", index: 1 });
    host.ensureApplied = async () => {
      host.model.removeSteps(["transform-1"]);
      return true;
    };
    expect(await insertEntry(host, pick("kind:wait"), request)).toBe("gone");
    expect(host.calls).toEqual([`notify ${GONE}`]);
    expect(ids(host.model)).toEqual(["transform-2"]);
  });

  it("refuses a common step or published action whose place moves while it waits", async () => {
    for (const id of [
      "common:weave-http:call-api",
      "action:sql.lookup@1.1.0",
    ]) {
      const host = fakeHost();
      host.model.insert("transform");
      host.model.insert("transform");
      const request = fresh(host, { owner: "root", index: 1 });
      const asked: boolean[] = [];
      const wait = async (still: () => boolean) => {
        host.model.removeSteps(["transform-1"]);
        asked.push(still());
        return still() ? "x" : "";
      };
      host.insertCommon = (_common, _at, _connector, still) => wait(still);
      host.insertPublished = (_uses, _id, _at, still) => wait(still);
      expect(await insertEntry(host, pick(id), request), id).toBe("gone");
      expect(asked, id).toEqual([false]);
      expect(host.calls, id).toEqual([`notify ${GONE}`]);
    }
  });

  it("refuses a disabled entry and changes nothing", async () => {
    const host = fakeHost();
    const off = buildCatalog(catalogInput());
    expect(
      await insertEntry(
        host,
        entryById(off, "kind:forEach"),
        fresh(host, { owner: "root", index: 0 }),
      ),
    ).toBe("refused");
    expect(ids(host.model)).toEqual([]);
  });
});
