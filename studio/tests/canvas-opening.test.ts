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
import { afterAll, describe, expect, it, vi } from "vitest";
vi.hoisted(() => vi.stubGlobal("document", { addEventListener: vi.fn() }));
import { StepDetailsMount } from "../src/app/editor/ndv/step-details-mount";
import { App } from "../src/app/app";
import { StructuredCanvasAdapter } from "../src/app/model";
import { CanvasView } from "../src/app/editor/canvas/canvas-view";
import {
  StepDetailsService,
  openRequest,
} from "../src/app/editor/ndv/step-details-service";
afterAll(() => vi.unstubAllGlobals());

function fixture(id = "lookup", badge = false) {
  const service = new StepDetailsService();
  const selected: string[] = [];
  const view = Object.create(CanvasView.prototype) as CanvasView;
  const target = {
    closest(selector: string): unknown {
      if (selector === ".tile-issue") return badge ? target : null;
      if (
        selector === ".tile-body" ||
        selector === "[data-action]" ||
        selector === "[data-tile]"
      )
        return target;
      return null;
    },
    matches: () => false,
    classList: { contains: (name: string) => name === "tile-body" },
    dataset: { action: badge ? "issues" : "tile", tile: id },
    getAttribute: () => id,
  };
  Object.assign(view, {
    suppressClick: false,
    places: () => new Map([["lookup", { id: "lookup", owner: "root" }]]),
    host: () => ({
      selectStep: async (step: string, open: boolean) => {
        selected.push(step);
        if (open) service.open(openRequest(step));
      },
      openStep: async (step: string, focus: string) =>
        service.open(openRequest(step, { revealAll: focus === "issues" })),
      openWorkflowSection: async (section: string) =>
        service.open(
          openRequest(section === "spec/inputSchema" ? "$trigger" : "$end"),
        ),
    }),
  });
  const event = (detail: number, extra = {}) =>
    ({ target, detail, ...extra }) as unknown as MouseEvent;
  return {
    view,
    service,
    selected,
    target: target as unknown as HTMLElement,
    event,
  };
}

describe("canvas opening dispatch", () => {
  for (const id of ["lookup", "$trigger:manual", "$end"]) {
    it(`opens ${id} exactly once for the full double-click sequence`, () => {
      const { view, service, event } = fixture(id);
      view.click(event(1));
      expect(service.opening()).toBe(0);
      view.click(event(2));
      expect(service.opening()).toBe(0);
      view.doubleClick(event(2));
      expect(service.opening()).toBe(1);
      expect(service.request()?.target).toBe(
        id.startsWith("$trigger:") ? "$trigger" : id,
      );
    });
    it(`opens ${id} once for the keyboard command`, () => {
      const { view, service, target } = fixture(id);
      expect(view.run("openStepDetails", target)).toBe(true);
      expect(service.opening()).toBe(1);
    });
  }
  it("a modified double-click does not open details", () => {
    const { view, service, event } = fixture();
    for (const modifier of ["shiftKey", "ctrlKey", "metaKey"])
      view.doubleClick(event(2, { [modifier]: true }));
    expect(service.opening()).toBe(0);
  });
  it("the completed drag click does not select or open", () => {
    const { view, service, selected, event } = fixture();
    Object.assign(view, { suppressClick: true });
    view.click(event(1));
    expect(selected).toEqual([]);
    expect(service.opening()).toBe(0);
  });
  it("Space opens on release exactly once, while a pan or modified Space does not open", () => {
    const { view, service, target } = fixture();
    const key = (type: string, extra = {}) =>
      ({
        key: " ",
        type,
        target,
        preventDefault: vi.fn(),
        ...extra,
      }) as unknown as KeyboardEvent;
    view.trackSpace(key("keydown"));
    expect(service.opening()).toBe(0);
    view.trackSpace(key("keyup"));
    expect(service.opening()).toBe(1);
    view.trackSpace(key("keydown"));
    Object.assign(view, { spacePanned: true });
    view.trackSpace(key("keyup"));
    expect(service.opening()).toBe(1);
    view.trackSpace(key("keydown", { shiftKey: true }));
    view.trackSpace(key("keyup", { shiftKey: true }));
    expect(service.opening()).toBe(1);
  });
  it("an issue badge opens exactly once without selecting or falling through", () => {
    const { view, service, selected, event } = fixture("lookup", true);
    view.click(event(1));
    expect(service.opening()).toBe(1);
    expect(service.request()?.revealAll).toBe(true);
    expect(selected).toEqual([]);
  });
});

describe("dialog focus-return ownership", () => {
  for (const change of [
    "selection",
    "workflow",
    "model",
    "request",
    "reopen-close",
    "canvas",
    "destroy",
  ] as const)
    it(`refuses a delayed return after a changed ${change}`, () => {
      const details = new StepDetailsService();
      details.open(openRequest("lookup"));
      const model = { opened: 1, selected: "lookup" };
      const host = { model };
      const lifetime = { destroyed: false };
      let current: (() => boolean) | undefined;
      let canvas: unknown = {
        restoreFocus: (_target: string, owns: () => boolean) => {
          current = owns;
        },
      };
      const mount = Object.create(
        StepDetailsMount.prototype,
      ) as StepDetailsMount;
      Object.assign(mount, {
        details,
        host: () => host,
        canvas: () => canvas,
        lifetime,
      });
      mount.closed("lookup");
      expect(current!()).toBe(true);
      if (change === "selection") model.selected = "another";
      if (change === "workflow") model.opened++;
      if (change === "model") host.model = { ...model };
      if (change === "request" || change === "reopen-close")
        details.open(openRequest("lookup"));
      if (change === "reopen-close") details.close();
      if (change === "canvas") canvas = null;
      if (change === "destroy") lifetime.destroyed = true;
      expect(current!()).toBe(false);
    });
});

function renamedInspector() {
  const app = Object.create(App.prototype) as App;
  const model = new StructuredCanvasAdapter();
  const step = model.insert("wait");
  const other = model.insert("wait");
  model.selected = step.id;
  Object.assign(app, {
    model,
    stepDetails: new StepDetailsService(),
    editorNext: true,
    view: "designer",
    cdr: { markForCheck() {} },
    changed() {},
    ensureApplied: async () => true,
    inspectorBuffer: JSON.stringify(step),
    inspectorTimer: null,
    pendingInspector: {
      scope: "rename",
      id: step.id,
      text: "pause",
      key: "id",
      opened: model.opened,
    },
  });
  Object.defineProperty(app, "editingLocked", { value: false });
  Object.defineProperty(app, "nodes", { get: () => app.model.nodes() });
  app.flushInspector();
  expect(model.selected).toBe("pause");
  return { app, model, old: step.id, other: other.id };
}

describe("opening after an inspector rename", () => {
  it("follows the successful rename once, then refuses the obsolete ID", async () => {
    const { app, old } = renamedInspector();
    await app.openStep(old, "details");
    expect(app.stepDetails.request()?.target).toBe("pause");
    expect(app.stepDetails.opening()).toBe(1);
    app.stepDetails.close();
    await app.openStep(old, "details");
    expect(app.stepDetails.isOpen()).toBe(false);
    expect(app.stepDetails.opening()).toBe(1);
  });
  for (const change of [
    "delete",
    "replace",
    "edit",
    "continued edit",
    "model",
    "navigation",
    "selection",
  ] as const)
    it(`does not follow stale rename provenance after ${change}`, async () => {
      const { app, model, old, other } = renamedInspector();
      if (change === "delete") {
        model.remove("pause");
        model.selected = other;
      }
      if (change === "replace") model.replace(model.definition);
      if (change === "edit" || change === "continued edit") {
        const edit = () =>
          model.update(
            "pause",
            JSON.stringify({ id: "pause", kind: "wait", durationSeconds: 90 }),
          );
        if (change === "continued edit")
          model.continueEdit(model.revision, edit);
        else edit();
      }
      if (change === "model") {
        app.model = new StructuredCanvasAdapter();
        app.model.insert("wait", "root", 0, { id: "pause" });
      }
      if (change === "navigation") app.view = "home";
      if (change === "selection") model.selected = other;
      await app.openStep(old, "details");
      expect(app.stepDetails.isOpen()).toBe(false);
    });
});
