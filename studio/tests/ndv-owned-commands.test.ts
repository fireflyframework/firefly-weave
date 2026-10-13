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
import { signal } from "@angular/core";
import { beforeAll, describe, expect, it } from "vitest";
import { loadKindRegistrations } from "../src/app/editor/ndv/kinds";
import { COMMON_ACTIONS } from "../src/app/editor/ndv/owned/common-actions";
import { insertOwnedStep } from "../src/app/editor/ndv/owned/insert-owned";
import {
  draftNote,
  exportNote,
  publishOwnedFirst,
  versionChangedOwned,
  type OwnedCommandHost,
} from "../src/app/editor/ndv/owned/owned-commands";
import type { OwnedRecipe } from "../src/app/editor/ndv/owned/owned-actions";
import {
  ownedActionsOf,
  withOwnedAction,
} from "../src/app/editor/ndv/owned/owned-store";
import { markPublished } from "../src/app/editor/ndv/owned/owned-versions";
import type { OwnedPublishOpen } from "../src/app/editor/ndv/step-details-service";
import { withStepNote } from "../src/app/editor/state/canvas-sidecar";
import { StructuredCanvasAdapter } from "../src/app/model";

const API = "order-intake.call-api@1.0.0";
const EMAIL = "order-intake.send-email@1.0.0";
const action = (id: string) => COMMON_ACTIONS.find((item) => item.id === id)!;

/** order-intake with Call an API (GET /orders) and Send email, as the shell sees it. */
function shell(change: Partial<OwnedCommandHost> = {}) {
  const model = new StructuredCanvasAdapter();
  model.definition.metadata.name = "order-intake";
  insertOwnedStep(model, action("send-email"), "root", 0);
  insertOwnedStep(model, action("call-api"), "root", 0);
  model.canvas = withOwnedAction(model.canvas, API, {
    ...ownedActionsOf(model.canvas)[API],
    pathTemplate: "/orders",
  } as OwnedRecipe);
  model.clearHistory();
  const notes: string[] = [];
  const checks: string[] = [];
  const ownedPublish = signal<OwnedPublishOpen | null>(null);
  const host: OwnedCommandHost = {
    model,
    editingLocked: false,
    actionVersions: [],
    stepDetails: { ownedPublish, mounted: 1 },
    error: "",
    errorCode: "",
    busy: "",
    can: () => true,
    perform: (edit) => {
      try {
        edit();
      } catch (error) {
        host.error = (error as Error).message;
      }
    },
    notify: (text) => void notes.push(text),
    refreshView: () => undefined,
    ...change,
  };
  const check = async () => {
    checks.push(host.busy);
    return true;
  };
  return { host, model, notes, checks, check, ownedPublish };
}
/** Publishes the call-api step's action, then adds a header, as Source would. */
function changedInSource(model: StructuredCanvasAdapter) {
  model.canvas = markPublished(model, API);
  const step = model.nodes().find((node) => node.step.id === "call-api")!.step;
  model.update(
    "call-api",
    JSON.stringify({
      ...step,
      with: {
        object: { headers: { object: { "X-Trace": { literal: "1" } } } },
      },
    }),
  );
}

beforeAll(async () => {
  await loadKindRegistrations();
});

describe("Publish…, Simulate and Save to file for a workflow's own actions", () => {
  it("says what Save to file leaves out", () => {
    const { model } = shell();
    expect(exportNote(model)).toBe(
      "These files don't keep the settings of 2 actions of this workflow's Call an API, email or file steps yet. Publish the workflow to keep them in the project.",
    );
    model.canvas = markPublished(model, API);
    expect(exportNote(model)).toContain("of 1 action of");
  });
  it("says what Save draft keeps only while the draft is open", () => {
    const { model } = shell();
    expect(draftNote(model)).toBe(
      "This draft keeps the settings of 2 actions of this workflow's Call an API, email or file steps only while it's open. Publish the workflow to keep them in the project.",
    );
    model.canvas = withStepNote(model.canvas, "call-api", { text: "Ask Ana" });
    expect(draftNote(model)).toMatch(
      / Its step notes also stay only while it's open\.$/,
    );
    model.canvas = markPublished(model, API);
    model.canvas = markPublished(model, EMAIL);
    expect(draftNote(model)).toBe(
      "This draft keeps its step notes only while it's open.",
    );
    model.canvas = withStepNote(model.canvas, "call-api", null);
    expect(draftNote(model)).toBe("");
  });
  it("gives an action changed outside step details its next version, unless a simulation runs", () => {
    const unchanged = shell();
    expect(versionChangedOwned(unchanged.host)).toBe(true);
    expect(unchanged.model.canUndo).toBe(false);
    const locked = shell({ editingLocked: true });
    changedInSource(locked.model);
    expect(versionChangedOwned(locked.host)).toBe(false);
    expect(locked.host.error).toBe(
      "Stop the simulation first: actions that changed after they were published need new versions.",
    );
    expect(locked.model.nodes()[0].step["uses"]).toBe(API);
    const { host, model, notes } = shell();
    changedInSource(model);
    expect(versionChangedOwned(host)).toBe(true);
    expect(model.nodes()[0].step["uses"]).toBe("order-intake.call-api@1.0.1");
    expect(notes).toEqual([
      "order-intake.call-api 1.0.0 is published, so this change starts version 1.0.1.",
    ]);
  });
  it("checks first, then waits for the dialog while busy, and goes on once every action is published", async () => {
    const { host, model, checks, check, ownedPublish } = shell();
    const going = publishOwnedFirst(host, "publish", check);
    await Promise.resolve();
    await Promise.resolve();
    expect(checks).toEqual([""]);
    expect(host.busy).toBe("publish");
    expect(ownedPublish()?.request).toEqual({
      purpose: "publish",
      workflow: { name: "order-intake", version: "1.0.0" },
    });
    ownedPublish()!.settle({ done: true, published: 2, source: model.source });
    expect(await going).toEqual({ result: "published", published: 2 });
    expect(host.busy).toBe("");
    expect(host.error).toBe("");
  });
  it("stops when the workflow changed after the actions were confirmed, keeping their count", async () => {
    const { host, model, check, ownedPublish } = shell();
    const confirmed = model.source;
    const going = publishOwnedFirst(host, "publish", check);
    await Promise.resolve();
    await Promise.resolve();
    // An edit lands while the actions publish.
    model.renameStep("send-email", "notify");
    ownedPublish()!.settle({ done: true, published: 2, source: confirmed });
    expect(await going).toEqual({ result: "stopped", published: 2 });
    expect(host.error).toBe(
      "The workflow changed while its actions were published, so it wasn't published. Its actions stay published; select Publish… again.",
    );
    const simulating = shell();
    const before = simulating.model.source;
    const started = publishOwnedFirst(
      simulating.host,
      "simulate",
      simulating.check,
    );
    await Promise.resolve();
    await Promise.resolve();
    simulating.model.renameStep("send-email", "notify");
    simulating
      .ownedPublish()!
      .settle({ done: true, published: 2, source: before });
    expect(await started).toEqual({ result: "stopped", published: 2 });
    expect(simulating.host.error).toBe(
      "The workflow changed while its actions were published, so the simulation didn't start. Select Simulate again.",
    );
  });
  it("stops without opening the dialog when the check fails, nothing is pending, or the account can't publish", async () => {
    const failed = shell();
    expect(
      await publishOwnedFirst(failed.host, "publish", async () => false),
    ).toEqual({
      result: "stopped",
      published: 0,
    });
    expect(failed.ownedPublish()).toBeNull();
    const none = shell();
    none.model.canvas = markPublished(none.model, API);
    none.model.canvas = markPublished(
      none.model,
      "order-intake.send-email@1.0.0",
    );
    expect(await publishOwnedFirst(none.host, "publish", none.check)).toEqual({
      result: "none",
      published: 0,
    });
    const denied = shell({ can: () => false });
    expect(
      await publishOwnedFirst(denied.host, "simulate", denied.check),
    ).toEqual({
      result: "stopped",
      published: 0,
    });
    expect(denied.host.error).toBe(
      "Simulating this workflow publishes the actions of its Call an API, email and file steps first, and your account can't publish actions in this workspace.",
    );
    expect(denied.ownedPublish()).toBeNull();
  });
  it("says why when no designer can show the dialog, or one is open already", async () => {
    const hidden = shell();
    hidden.host = {
      ...hidden.host,
      stepDetails: { ownedPublish: hidden.ownedPublish, mounted: 0 },
    };
    expect(
      await publishOwnedFirst(hidden.host, "publish", hidden.check),
    ).toEqual({
      result: "stopped",
      published: 0,
    });
    expect(hidden.host.error).toBe(
      "Open the workflow in the designer to publish its actions.",
    );
    const busy = shell();
    busy.ownedPublish.set({
      request: {
        purpose: "simulate",
        workflow: { name: "x", version: "1.0.0" },
      },
      settle: () => undefined,
    });
    expect(await publishOwnedFirst(busy.host, "publish", busy.check)).toEqual({
      result: "stopped",
      published: 0,
    });
    expect(busy.host.error).toBe("Wait for the current command to finish.");
  });
});
