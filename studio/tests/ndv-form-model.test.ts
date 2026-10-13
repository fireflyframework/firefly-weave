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
  featureReason,
  formState,
  hiddenChanges,
  paramIsDefault,
  resetParam,
  type FormEnv,
} from "../src/app/editor/ndv/params/form-model";
import {
  applyChange,
  type FormSubject,
} from "../src/app/editor/ndv/params/value-io";
import type {
  FormSpec,
  KindContext,
  ParamSpec,
} from "../src/app/editor/ndv/registry";
import { freshWorkflow, type Step } from "../src/app/model";

const http = (method: string): Step => ({
  id: "call-api-1",
  kind: "action",
  uses: "flow.call-api-1@1.0.0",
  with: { object: { body: { object: { note: { literal: "hi" } } } } },
  method,
});
const kind = (features: string[] = []): KindContext => ({
  workflow: freshWorkflow(),
  features,
  actionContract: () => null,
  tableContract: () => null,
  workflowContract: () => null,
});
const env = (
  step: Step,
  added: string[] = [],
  features: string[] = [],
): FormEnv => {
  const subject: FormSubject = {
    step,
    workflow: {
      ...freshWorkflow(),
      spec: { ...freshWorkflow().spec, steps: [step] },
    },
    action: null,
    roots: [["with"]],
  };
  return {
    subject,
    step,
    kind: kind(features),
    readOnly: null,
    added: new Set(added),
  };
};
const write = (s: Step) => s["method"] !== "GET";
const param = (id: string, extra: Partial<ParamSpec> = {}): ParamSpec => ({
  id,
  path: ["with", id],
  type: "text",
  label: id[0].toUpperCase() + id.slice(1),
  ...extra,
});
const form: FormSpec = {
  fields: [
    param("connection", { path: ["connection"] }),
    param("method", { path: ["method"] }),
    param("url"),
  ],
  options: [
    param("query", { type: "keyValue" }),
    param("headers", { type: "keyValue" }),
    param("body", {
      type: "keyValue",
      required: true,
      showWhen: write,
      hiddenReason: () => "Only for POST, PUT and PATCH",
    }),
    param("description", { feature: "text.concat" }),
  ],
};

describe("form model", () => {
  it("shows the default fields in order, and options only once added or required", () => {
    expect(
      formState(form, env(http("GET"))).fields.map((f) => f.spec.id),
    ).toEqual(["connection", "method", "url"]);
    expect(
      formState(form, env(http("POST"))).fields.map((f) => f.spec.id),
    ).toEqual(["connection", "method", "url", "body"]);
  });
  it("offers the other options in Add option, hidden and gated ones disabled with their reason", () => {
    expect(
      formState(form, env(http("GET"))).addable.map((o) => [
        o.spec.id,
        o.disabled,
      ]),
    ).toEqual([
      ["query", null],
      ["headers", null],
      ["body", "Only for POST, PUT and PATCH"],
      ["description", featureReason("text.concat")],
    ]);
    expect(featureReason("text.concat")).toBe(
      "Update the platform to use this (text.concat).",
    );
  });
  it("shows an option added in this session even at its default", () => {
    const state = formState(form, env(http("GET"), ["headers"]));
    expect(state.fields.map((f) => [f.spec.id, f.option])).toContainEqual([
      "headers",
      true,
    ]);
    expect(state.addable.map((o) => o.spec.id)).not.toContain("headers");
  });
  it("shows an option whose stored value differs from its default", () => {
    const step = {
      ...http("GET"),
      with: { object: { query: { literal: { limit: "25" } } } },
    };
    expect(formState(form, env(step)).fields.map((f) => f.spec.id)).toContain(
      "query",
    );
  });
  it("passes read-only reasons down, the dialog's first", () => {
    const locked = {
      ...env(http("GET")),
      readOnly: "Fix the source to edit this step.",
    };
    expect(formState(form, locked).fields[0].readOnly).toBe(
      "Fix the source to edit this step.",
    );
    const own: FormSpec = {
      fields: [param("url", { readOnly: () => "Set by the action." })],
    };
    expect(formState(own, env(http("GET"))).fields[0].readOnly).toBe(
      "Set by the action.",
    );
  });
  it("clears a field that a change hides, unless it keeps its value", () => {
    const before = formState(form, env(http("POST")));
    const afterEnv = env(http("GET"));
    const after = formState(form, afterEnv);
    const cleared = hiddenChanges(before, after, afterEnv, new Set());
    expect(cleared.labels).toEqual(["Body"]);
    expect(cleared.reasons).toEqual(["Only for POST, PUT and PATCH"]);
    expect(cleared.changes).toEqual([
      { scope: "step", path: ["with"], value: { literal: {} } },
    ]);
    const keep: FormSpec = {
      ...form,
      options: form.options!.map((o) =>
        o.id === "body" ? { ...o, whenHidden: "keep" as const } : o,
      ),
    };
    expect(
      hiddenChanges(
        formState(keep, env(http("POST"))),
        formState(keep, afterEnv),
        afterEnv,
        new Set(),
      ).changes,
    ).toEqual([]);
  });
  it("treats a value-less group as default when all its children are, and resets each child", () => {
    const group: ParamSpec = {
      id: "results",
      path: [],
      type: "fields",
      label: "Results",
      children: () => [
        param("cc", { type: "list" }),
        param("bcc", { type: "list" }),
      ],
    };
    const step = {
      ...http("GET"),
      with: { literal: { cc: ["a@x.test"], bcc: ["b@x.test"] } },
    };
    const e = env(step);
    expect(paramIsDefault(group, e)).toBe(false);
    expect(paramIsDefault(group, env(http("GET")))).toBe(true);
    const changes = resetParam(group, e);
    expect(changes.at(-1)).toEqual({
      scope: "step",
      path: ["with"],
      value: { literal: {} },
    });
  });

  it("lists an option again, with its default written, after it is removed", () => {
    const limit = param("limit", {
      path: ["limit"],
      type: "number",
      default: 2,
      whenRemoved: "default",
    });
    const plain = param("plain", {
      path: ["plain"],
      type: "number",
      default: 2,
    });
    const limited = { ...http("GET"), limit: 3, plain: 3 };
    const options: FormSpec = { fields: [], options: [limit, plain] };
    const e = env(limited);
    expect(formState(options, e).fields.map((f) => f.spec.id)).toEqual([
      "limit",
      "plain",
    ]);
    const changes = [...resetParam(limit, e), ...resetParam(plain, e)];
    expect(changes).toEqual([
      { scope: "step", path: ["limit"], value: 2 },
      { scope: "step", path: ["plain"], value: undefined },
    ]);
    const subject = changes.reduce(applyChange, e.subject);
    const after = formState(options, { ...e, subject, step: subject.step! });
    expect(after.fields).toEqual([]);
    expect(after.addable.map((o) => o.spec.id)).toEqual(["limit", "plain"]);
    expect(subject.step?.["limit"]).toBe(2);
    expect(subject.step).not.toHaveProperty("plain");
  });

  it("leaves alone a field the person removed on purpose", () => {
    const before = formState(form, env(http("POST")));
    const afterEnv = env(http("GET"));
    const after = formState(form, afterEnv);
    expect(
      hiddenChanges(before, after, afterEnv, new Set(["body"])).changes,
    ).toEqual([]);
  });
});
