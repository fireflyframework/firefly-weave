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
import { beforeAll, describe, expect, it } from "vitest";
import { loadKindRegistrations } from "../src/app/editor/ndv/kinds";
import { formEnv, stepSubject } from "../src/app/editor/ndv/params/form-env";
import { formState } from "../src/app/editor/ndv/params/form-model";
import {
  newConnectorRecipe,
  newHttpRecipe,
  type OwnedRecipe,
} from "../src/app/editor/ndv/owned/owned-actions";
import {
  ndvRegistry,
  type Json,
  type KindContext,
} from "../src/app/editor/ndv/registry";
import {
  applyChange,
  fixed,
  FormWriteError,
  mapped,
  readParam,
  resetChanges,
  writeParam,
} from "../src/app/editor/ndv/params/value-io";
import { createStep, freshWorkflow, type Step } from "../src/app/model";
import { lookupAction } from "./browser/support";

beforeAll(() => loadKindRegistrations());

const owned: Record<string, OwnedRecipe> = {
  ...Object.fromEntries(
    (["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"] as const).map(
      (method) => [
        `flow.${method.toLowerCase()}@1.0.0`,
        { ...newHttpRecipe(), method },
      ],
    ),
  ),
  "flow.post@1.0.0": { ...newHttpRecipe(), method: "POST" },
  "flow.send-email@1.0.0": newConnectorRecipe("weave-email@1.0.0", "send"),
  "flow.reply@1.0.0": newConnectorRecipe("weave-email@1.0.0", "reply"),
  ...Object.fromEntries(
    ["list", "read", "download", "write", "move", "delete"].flatMap(
      (action) => [
        [
          `flow.sftp-${action}@1.0.0`,
          newConnectorRecipe("weave-sftp@1.0.0", action),
        ],
        [
          `flow.drive-${action}@1.0.0`,
          newConnectorRecipe("weave-google-drive@1.0.0", action),
        ],
      ],
    ),
  ),
};
const sendDocument = {
  spec: { sideEffect: "non_idempotent", timeoutSeconds: 60 },
};
const ctx = (workflow = freshWorkflow()): KindContext => ({
  workflow,
  features: [],
  actionContract: (uses) =>
    uses === "sql.lookup@1.0.0"
      ? (lookupAction as Json)
      : uses === "flow.send-email@1.0.0"
        ? (sendDocument as Json)
        : null,
  tableContract: () => null,
  workflowContract: () => null,
  ownedAction: (uses) => (owned[uses] as unknown as Json) ?? null,
});
const action = (uses: string, extra: Record<string, unknown> = {}): Step => ({
  ...createStep("action", "call-1"),
  uses,
  ...extra,
});
function shown(step: Step, which: "form" | "settings" = "form", c = ctx()) {
  const descriptor = ndvRegistry.kind("action")!;
  const recipe = c.ownedAction?.(String(step["uses"])) ?? null;
  const spec = descriptor[which]!(step, c);
  const state = formState(
    spec,
    formEnv(
      stepSubject(descriptor, step, c, recipe as unknown as Json),
      step,
      c,
    ),
  );
  return {
    labels: state.fields.map((f) => f.spec.label),
    addable: state.addable.map((o) => [o.spec.label, o.disabled]),
    state,
  };
}

describe("action forms", () => {
  it("shows Connection, Method and URL for Call an API, and Body for requests that send one", () => {
    expect(shown(action("flow.get@1.0.0")).labels).toEqual([
      "Connection",
      "Method",
      "URL",
    ]);
    expect(shown(action("flow.post@1.0.0")).labels).toEqual([
      "Connection",
      "Method",
      "URL",
      "Body",
    ]);
    expect(shown(action("flow.get@1.0.0")).addable).toEqual([
      ["Query parameters", null],
      ["Headers", null],
      ["Body", "Only for POST, PUT and PATCH"],
      ["Response sample", null],
      ["Accepted status codes", null],
      ["Description", null],
    ]);
  });
  it("labels each method with what it does", () => {
    const method = shown(action("flow.get@1.0.0")).state.fields[1].spec;
    expect(method.choices).toContainEqual({
      value: "GET",
      label: "GET · Reads data",
    });
    expect(method.choices).toContainEqual({
      value: "PATCH",
      label: "PATCH · Changes data",
    });
  });
  it("shows the email fields", () => {
    expect(shown(action("flow.send-email@1.0.0")).labels).toEqual([
      "Connection",
      "To",
      "Subject",
      "Message",
    ]);
    expect(
      shown(action("flow.send-email@1.0.0")).addable.map(([label]) => label),
    ).toEqual(["Cc", "Bcc", "HTML version", "Attachments"]);
    expect(shown(action("flow.reply@1.0.0")).labels).toEqual([
      "Connection",
      "Reply to message",
      "Conversation",
      "Message",
    ]);
  });
  it("shows the file fields, with a name on drives", () => {
    const labels = (op: string, place = "sftp") =>
      shown(action(`flow.${place}-${op}@1.0.0`)).labels;
    expect(labels("list")).toEqual(["Connection", "Folder"]);
    expect(labels("read")).toEqual(["Connection", "Path"]);
    expect(labels("download")).toEqual(["Connection", "Path"]);
    expect(labels("write")).toEqual(["Connection", "File", "Destination"]);
    expect(labels("write", "drive")).toEqual([
      "Connection",
      "File",
      "Destination",
      "Name",
    ]);
    expect(labels("move")).toEqual(["Connection", "Path", "Destination"]);
    expect(labels("move", "drive")).toEqual([
      "Connection",
      "Item ID",
      "Destination",
      "Name",
    ]);
    expect(labels("delete")).toEqual(["Connection", "Path"]);
  });
  it("shows a published action, its connection and its required inputs", () => {
    const step = action("sql.lookup@1.0.0", { with: { literal: {} } });
    expect(shown(step).labels).toEqual(["Action", "Connection", "Parameters"]);
    expect(shown(step).addable.map(([label]) => label)).toEqual([
      "Limit",
      "Mode",
      "Label",
      "Tags",
      "Dry run",
      "Options",
    ]);
    expect(
      shown(action("sql.lookup@1.0.0", { with: { ref: "/input" } })).labels,
    ).toEqual(["Action", "Connection", "Input"]);
    expect(shown(createStep("action", "new-1")).labels).toEqual(["Action"]);
  });
});

describe("action settings", () => {
  it("lets a workflow-owned GET request retry and caps its timeout", () => {
    const { state } = shown(action("flow.get@1.0.0"), "settings");
    expect(state.fields.map((f) => [f.spec.label, f.readOnly])).toEqual([
      ["Retry on fail", null],
      ["Timeout", null],
    ]);
    expect(state.fields[1].spec.max).toBe(30);
  });
  it("never retries a request or a send that changes data", () => {
    const post = shown(action("flow.post@1.0.0"), "settings").state.fields[0];
    expect(post.readOnly).toBe(
      "Changes data, so Weave never retries it automatically.",
    );
    const send = shown(action("flow.send-email@1.0.0"), "settings").state
      .fields[0];
    expect(send.readOnly).toBe(
      "Changes data, so Weave never retries it automatically.",
    );
  });
  it("shows a published action's retry and timeout as set by the action", () => {
    const { state } = shown(action("sql.lookup@1.0.0"), "settings");
    expect(state.fields.map((f) => f.readOnly)).toEqual([
      "Set by sql.lookup@1.0.0.",
      "Set by sql.lookup@1.0.0.",
    ]);
  });
  it("has no retry or timeout before an action is chosen", () => {
    expect(shown(createStep("action", "new-1"), "settings").labels).toEqual([]);
  });
});

describe("action form preservation", () => {
  it("sends a body only with POST, PUT and PATCH, while all other changing methods disable retry", () => {
    for (const [method, body, retry] of [
      ["GET", false, true],
      ["HEAD", false, true],
      ["POST", true, false],
      ["PUT", true, false],
      ["PATCH", true, false],
      ["DELETE", false, false],
    ] as const) {
      const step = action(`flow.${method.toLowerCase()}@1.0.0`);
      expect(shown(step).labels.includes("Body"), method).toBe(body);
      expect(
        shown(step, "settings").state.fields[0].readOnly === null,
        method,
      ).toBe(retry);
    }
  });

  it("keeps an existing connection editable when its action contract is unavailable", () => {
    expect(
      shown(action("missing@1.0.0", { connection: "api" })).labels,
    ).toEqual(["Action", "Connection"]);
  });

  it("opening whole mappings and opaque values never rewrites the input", () => {
    for (const value of [
      { ref: "/input" },
      { op: { name: "future", args: [{ ref: "/input" }] } },
      { unknown: ["unchanged"] },
    ]) {
      const step = action("sql.lookup@1.0.0", { with: value });
      const before = structuredClone(step);
      const view = shown(step);
      expect(step).toEqual(before);
      const input = view.state.fields.find((f) => f.spec.id === "input")!.spec;
      const c = ctx();
      const subject = stepSubject(ndvRegistry.kind("action")!, step, c, null);
      expect(readParam(subject, input)).toEqual(mapped(value));
      const child = input.children!(step, c)[0];
      expect(() => writeParam(subject, child, fixed({}))).toThrow(
        FormWriteError,
      );
      expect(step).toEqual(before);
    }
  });

  it("writes an input field without dropping unknown keys or sibling mappings", () => {
    const step = action("sql.lookup@1.0.0", {
      with: {
        object: {
          parameters: { literal: {} },
          label: { ref: "/input/name" },
          extra: { op: { name: "future", args: [] } },
        },
      },
    });
    const c = ctx();
    const param = shown(step).state.fields.find(
      (f) => f.spec.path.join("/") === "with/parameters",
    )!.spec;
    const subject = stepSubject(ndvRegistry.kind("action")!, step, c, null);
    const result = writeParam(subject, param, fixed({ id: 7 })).reduce(
      applyChange,
      subject,
    );
    expect(result.step!["with"]).toEqual({
      object: {
        parameters: { literal: { id: 7 } },
        label: { ref: "/input/name" },
        extra: { op: { name: "future", args: [] } },
      },
    });
    expect(step["with"]).toEqual({
      object: {
        parameters: { literal: {} },
        label: { ref: "/input/name" },
        extra: { op: { name: "future", args: [] } },
      },
    });
  });

  it("removing accepted status codes restores a usable request default", () => {
    const step = action("flow.get@1.0.0");
    const c = ctx();
    const recipe = { ...newHttpRecipe(), statuses: [201] };
    const param = ndvRegistry.kind("action")!.form!(step, c).options!.find(
      (p) => p.id === "statuses",
    )!;
    const subject = stepSubject(
      ndvRegistry.kind("action")!,
      step,
      c,
      recipe as unknown as Json,
    );
    expect(
      resetChanges(subject, param).reduce(applyChange, subject).action,
    ).toEqual({ ...recipe, statuses: [200] });
  });

  it("keeps saved retry unchanged until the connector contract supplies its policy", () => {
    const step = action("flow.sftp-read@1.0.0");
    const c = ctx();
    const recipe = {
      ...owned["flow.sftp-read@1.0.0"],
      retry: { maxAttempts: 4, initialDelaySeconds: 2, maxDelaySeconds: 40 },
    };
    c.ownedAction = () => recipe as unknown as Json;
    const before = structuredClone(recipe);
    const missing = shown(step, "settings", c).state.fields[0];
    expect(missing.readOnly).toBe(
      "Load the action to check whether it can retry.",
    );
    const loaded = shown(step, "settings", {
      ...c,
      actionContract: () => ({ spec: { sideEffect: "read_only" } }),
    }).state.fields[0];
    expect(loaded.readOnly).toBeNull();
    const writes = shown(step, "settings", {
      ...c,
      actionContract: () => ({ spec: { sideEffect: "non_idempotent" } }),
    }).state.fields[0];
    expect(writes.readOnly).toBe(
      "Changes data, so Weave never retries it automatically.",
    );
    expect(recipe).toEqual(before);
  });

  it("keeps the AI task timeout with its profile", () => {
    const step = createStep("llm", "ai-1");
    expect(ndvRegistry.kind("llm")!.settings!(step, ctx())).toEqual({
      fields: [],
    });
  });
});
