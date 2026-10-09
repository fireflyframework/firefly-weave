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
import {
  DEFAULT_RETRY,
  connectorDocument,
  httpBuildRequest,
  isWriteMethod,
  nameOfUses,
  newConnectorRecipe,
  newHttpRecipe,
  ownedName,
  ownedUses,
  pathPlaceholders,
  recipeOf,
  sendsBody,
  sideEffectOf,
  withRetry,
  type HttpRecipe,
} from "../src/app/editor/ndv/owned/owned-actions";
import {
  ownedActionsOf,
  renameOwnedAction,
  usesOwning,
  withOwnedAction,
} from "../src/app/editor/ndv/owned/owned-store";
import type { KindContext } from "../src/app/editor/ndv/registry";
import { emptyCanvas } from "../src/app/editor/state/canvas-sidecar";
import type { Step } from "../src/app/model";

const sendTemplate = {
  apiVersion: "weave/v1alpha1",
  kind: "Action",
  metadata: { name: "weave-email-send", version: "1.0.0" },
  spec: {
    implementation: {
      kind: "connector",
      uses: "weave-email@1.0.0",
      action: "send",
    },
    inputSchema: { type: "object" },
    outputSchema: { type: "object" },
    sideEffect: "non_idempotent",
    timeoutSeconds: 60,
    connection: { connector: "weave-email@1.0.0" },
  },
};

describe("workflow-owned actions", () => {
  it("names an owned action after the workflow and the step", () => {
    expect(ownedName("order-intake", "get-customer-orders", new Set())).toBe(
      "order-intake.get-customer-orders",
    );
    expect(
      ownedName("order-intake", "get", new Set(["order-intake.get"])),
    ).toBe("order-intake.get-2");
    expect(ownedName("w".repeat(100), "s".repeat(100), new Set())).toHaveLength(
      128,
    );
    expect(ownedUses("order-intake.get")).toBe("order-intake.get@1.0.0");
    expect(nameOfUses("order-intake.get@1.0.0")).toBe("order-intake.get");
    expect(nameOfUses("no-version")).toBe("no-version");
  });
  it("keeps a name that is cut short free of a trailing dot or hyphen", () => {
    expect(ownedName("w".repeat(127), "step", new Set())).toBe("w".repeat(127));
    expect(ownedName("flow", "step-", new Set())).toBe("flow.step");
  });
  it("sends a body for POST, PUT and PATCH only, and changes data for anything but GET and HEAD", () => {
    for (const method of ["POST", "PUT", "PATCH"]) {
      expect(sendsBody(method)).toBe(true);
      expect(isWriteMethod(method)).toBe(true);
    }
    for (const method of ["GET", "HEAD"]) {
      expect(sendsBody(method)).toBe(false);
      expect(isWriteMethod(method)).toBe(false);
    }
    expect(sendsBody("DELETE")).toBe(false);
    expect(isWriteMethod("DELETE")).toBe(true);
  });
  it("finds path placeholders", () => {
    expect(
      pathPlaceholders("/customers/{customerId}/orders/{orderId}"),
    ).toEqual(["customerId", "orderId"]);
    expect(pathPlaceholders("/plain")).toEqual([]);
  });
  it("builds the host request from the recipe and the step's input keys", () => {
    const recipe: HttpRecipe = {
      ...newHttpRecipe(),
      pathTemplate: "/customers/{customerId}/orders",
    };
    const stepWith = {
      object: {
        path: { object: { customerId: { ref: "/input/customerId" } } },
        query: { literal: { limit: "25" } },
        headers: { object: { "X-Trace": { ref: "/input/trace" } } },
        body: { literal: { note: "hi" } },
      },
    };
    expect(httpBuildRequest("flow.get", recipe, stepWith)).toEqual({
      name: "flow.get",
      version: "1.0.0",
      method: "GET",
      pathTemplate: "/customers/{customerId}/orders",
      parameters: [
        { name: "limit", location: "query", type: "string" },
        { name: "X-Trace", location: "header", type: "string" },
      ],
      statuses: [200],
      timeoutSeconds: 30,
    });
    expect(
      httpBuildRequest("flow.get", { ...recipe, method: "POST" }, stepWith),
    ).toMatchObject({
      method: "POST",
      bodySchema: {
        type: "object",
        properties: { note: {} },
        required: ["note"],
        additionalProperties: false,
      },
    });
    expect(
      httpBuildRequest("flow.get", { ...recipe, pathTemplate: "" }, stepWith),
    ).toBeNull();
    expect(
      httpBuildRequest(
        "flow.get",
        { ...recipe, pathTemplate: "customers" },
        stepWith,
      ),
    ).toBeNull();
    expect(
      httpBuildRequest(
        "flow.get",
        { ...recipe, pathTemplate: "//elsewhere.example/x" },
        stepWith,
      ),
    ).toBeNull();
  });
  it("describes a body only for the methods that send one", () => {
    const recipe = { ...newHttpRecipe(), pathTemplate: "/orders" };
    const stepWith = { object: { body: { literal: { note: "hi" } } } };
    for (const method of ["PUT", "PATCH"] as const)
      expect(
        httpBuildRequest("flow.save", { ...recipe, method }, stepWith),
      ).toHaveProperty("bodySchema.required", ["note"]);
    for (const method of ["GET", "HEAD", "DELETE"] as const)
      expect(
        httpBuildRequest("flow.save", { ...recipe, method }, stepWith),
      ).not.toHaveProperty("bodySchema");
    expect(
      httpBuildRequest(
        "flow.save",
        { ...recipe, method: "POST" },
        {
          object: { body: { literal: {} } },
        },
      ),
    ).not.toHaveProperty("bodySchema");
  });
  it("carries the description, the response sample and the statuses of the recipe", () => {
    const recipe: HttpRecipe = {
      ...newHttpRecipe(),
      pathTemplate: "/orders",
      description: "Orders of a customer",
      responseSample: { id: "o-1" },
      statuses: [200, 404],
      timeoutSeconds: 10,
    };
    expect(httpBuildRequest("flow.get", recipe, undefined)).toMatchObject({
      description: "Orders of a customer",
      responseSample: { id: "o-1" },
      statuses: [200, 404],
      timeoutSeconds: 10,
      parameters: [],
    });
    expect(
      httpBuildRequest("flow.get", { ...recipe, statuses: [] }, undefined),
    ).toMatchObject({ statuses: [200] });
  });
  it("reads no keys from an input that is mapped as a whole or isn't an object, and never fails on it", () => {
    const recipe = { ...newHttpRecipe(), pathTemplate: "/orders" };
    for (const stepWith of [
      undefined,
      null,
      "text",
      7,
      [1, 2],
      { ref: "/input" },
      { literal: "text" },
      { object: { query: { ref: "/input/query" } } },
    ])
      expect(httpBuildRequest("flow.get", recipe, stepWith)).toMatchObject({
        parameters: [],
      });
  });
  it("lets a real fault in reading the input reach the caller", () => {
    const recipe = { ...newHttpRecipe(), pathTemplate: "/orders" };
    const loop: { object: Record<string, unknown> } = { object: {} };
    loop.object["query"] = loop;
    expect(() => httpBuildRequest("flow.get", recipe, loop)).toThrow(
      RangeError,
    );
  });
  it("copies a connector action as a document of its own, with timeout and retry", () => {
    const send = connectorDocument(
      "flow.send-email",
      newConnectorRecipe("weave-email@1.0.0", "send"),
      sendTemplate,
    );
    expect((send as { metadata: unknown }).metadata).toEqual({
      name: "flow.send-email",
      version: "1.0.0",
    });
    expect(sideEffectOf(send)).toBe("non_idempotent");
    const slow = connectorDocument(
      "flow.send-email",
      {
        ...newConnectorRecipe("weave-email@1.0.0", "send"),
        timeoutSeconds: 120,
        description: "Used by step send-email of flow",
      },
      sendTemplate,
    ) as {
      spec: Record<string, unknown>;
      metadata: Record<string, unknown>;
    };
    expect(slow.spec["timeoutSeconds"]).toBe(120);
    expect(slow.metadata["description"]).toBeUndefined();
    expect(
      (slow.spec["inputSchema"] as Record<string, unknown>)["description"],
    ).toBe("Used by step send-email of flow");
    expect(sendTemplate.metadata.name).toBe("weave-email-send");
    expect(sendTemplate.spec.timeoutSeconds).toBe(60);
  });
  it("retries only actions that read data", () => {
    const read = { spec: { sideEffect: "read_only" } };
    expect(withRetry(read, DEFAULT_RETRY)).toEqual({
      spec: { sideEffect: "read_only", retry: DEFAULT_RETRY },
    });
    expect(withRetry(sendTemplate, DEFAULT_RETRY)).toEqual(sendTemplate);
    expect(withRetry(read, undefined)).toEqual(read);
    expect(read).toEqual({ spec: { sideEffect: "read_only" } });
  });
  it("reads the recipe of the action a step calls from the editor's context", () => {
    const recipe = { ...newHttpRecipe(), pathTemplate: "/orders" };
    const ctx = {
      ownedAction: (uses: string) =>
        uses === "flow.get@1.0.0" ? (recipe as never) : null,
    } as unknown as KindContext;
    const step = (uses: unknown) => ({ id: "s", kind: "action", uses }) as Step;
    expect(recipeOf(step("flow.get@1.0.0"), ctx)).toEqual(recipe);
    expect(recipeOf(step("other@1.0.0"), ctx)).toBeNull();
    expect(recipeOf(step(undefined), ctx)).toBeNull();
    expect(recipeOf(step("flow.get@1.0.0"), {} as KindContext)).toBeNull();
  });
});

describe("the owned action store", () => {
  it("keeps recipes in the canvas sidecar by action name, and reads them by uses", () => {
    const next = withOwnedAction(
      emptyCanvas(),
      "flow.get@1.0.0",
      newHttpRecipe(),
    );
    expect(next.ownedActions).toEqual(["flow.get"]);
    expect(Object.keys(ownedActionsOf(next))).toEqual(["flow.get@1.0.0"]);
    expect(ownedActionsOf(next)["flow.get@1.0.0"]).toEqual(newHttpRecipe());
    expect(usesOwning(next, "flow.get@1.0.0")).toBe(true);
    expect(usesOwning(next, "flow.get@2.0.0")).toBe(false);
    expect(usesOwning(next, undefined)).toBe(false);
    const moved = renameOwnedAction(next, "flow.get@1.0.0", "flow.fetch@1.0.0");
    expect(Object.keys(ownedActionsOf(moved))).toEqual(["flow.fetch@1.0.0"]);
    expect(
      ownedActionsOf(withOwnedAction(next, "flow.get@1.0.0", null)),
    ).toEqual({});
  });
  it("returns the same canvas when nothing changes", () => {
    const canvas = withOwnedAction(
      emptyCanvas(),
      "flow.get@1.0.0",
      newHttpRecipe(),
    );
    expect(withOwnedAction(canvas, "flow.get@1.0.0", newHttpRecipe())).toBe(
      canvas,
    );
    expect(withOwnedAction(canvas, "flow.other@1.0.0", null)).toBe(canvas);
    expect(renameOwnedAction(canvas, "flow.get@1.0.0", "flow.get@1.0.0")).toBe(
      canvas,
    );
  });
  it("says why when the canvas refuses a name, and when a name is taken", () => {
    expect(() =>
      withOwnedAction(emptyCanvas(), "bad name@1.0.0", newHttpRecipe()),
    ).toThrow(/letters, numbers, dots, underscores or hyphens/);
    const two = withOwnedAction(
      withOwnedAction(emptyCanvas(), "flow.a@1.0.0", newHttpRecipe()),
      "flow.b@1.0.0",
      newHttpRecipe(),
    );
    expect(() =>
      renameOwnedAction(two, "flow.a@1.0.0", "flow.b@1.0.0"),
    ).toThrow("Another action is already named flow.b.");
    expect(() =>
      renameOwnedAction(two, "flow.a@1.0.0", "bad name@1.0.0"),
    ).toThrow(/letters, numbers, dots, underscores or hyphens/);
  });
  it("leaves actions that aren't at the owned version alone", () => {
    const canvas = withOwnedAction(
      emptyCanvas(),
      "flow.get@1.0.0",
      newHttpRecipe(),
    );
    expect(withOwnedAction(canvas, "flow.get@2.0.0", null)).toBe(canvas);
    expect(() =>
      withOwnedAction(canvas, "flow.get@2.0.0", newHttpRecipe()),
    ).toThrow("Only an action at version 1.0.0 can belong to this workflow.");
    expect(
      renameOwnedAction(canvas, "flow.get@2.0.0", "flow.fetch@2.0.0"),
    ).toBe(canvas);
    expect(() =>
      renameOwnedAction(canvas, "flow.get@1.0.0", "flow.fetch@2.0.0"),
    ).toThrow("Only an action at version 1.0.0 can belong to this workflow.");
    expect(Object.keys(ownedActionsOf(canvas))).toEqual(["flow.get@1.0.0"]);
  });
});
