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
// A loop's settings in step details: one field, Items, and the rest behind
// Add option, each with the language's default.
import "@angular/compiler";
import { beforeAll, describe, expect, it } from "vitest";
import { referenceView } from "../src/app/forms/ui/reference-combobox";
import { headerSubtitle } from "../src/app/editor/ndv/header";
import { fieldFor, type PlacedSpec } from "../src/app/editor/ndv/issues";
import { loadKindRegistrations } from "../src/app/editor/ndv/kinds";
import { fieldRules } from "../src/app/editor/ndv/params/field-rules";
import { formEnv, stepSubject } from "../src/app/editor/ndv/params/form-env";
import { formState, resetParam } from "../src/app/editor/ndv/params/form-model";
import {
  applyChange,
  fixed,
  readEntries,
  readParam,
  writeParam,
} from "../src/app/editor/ndv/params/value-io";
import {
  ndvRegistry,
  type KindContext,
  type ParamSpec,
} from "../src/app/editor/ndv/registry";
import { onErrorText } from "../src/app/editor/ndv/settings/settings-rules";
import { stepPath } from "../src/app/editor/ndv/edits";
import { createStep, freshWorkflow, type Step } from "../src/app/model";

beforeAll(() => loadKindRegistrations());

const context = (step: Step): KindContext => {
  const workflow = freshWorkflow();
  workflow.spec.steps.push(step);
  return {
    workflow,
    features: ["flow.forEach"],
    actionContract: () => null,
    tableContract: () => null,
    workflowContract: () => null,
  };
};
const loop = (extra: Record<string, unknown> = {}): Step => ({
  ...createStep("forEach", "orders"),
  ...extra,
});
function shown(step: Step) {
  const ctx = context(step);
  const descriptor = ndvRegistry.kind("forEach")!;
  const form = descriptor.form!(step, ctx);
  const subject = stepSubject(descriptor, step, ctx, null);
  const env = formEnv(subject, step, ctx);
  const state = formState(form, env);
  return {
    form,
    subject,
    env,
    shown: state.fields.map((f) => f.spec.label),
    addable: state.addable.map((o) => o.spec.label),
    spec: (id: string): ParamSpec =>
      [...form.fields, ...(form.options ?? [])].find((s) => s.id === id)!,
  };
}

describe("a loop's settings", () => {
  it("shows Items and offers the rest under Add option", () => {
    const { shown: labels, addable } = shown(loop());
    expect(labels).toEqual(["Items"]);
    expect(addable).toEqual([
      "At a time",
      "Up to",
      "Results",
      "Result per item",
    ]);
    // An option shows once it holds something other than its default.
    expect(shown(loop({ concurrency: 4, collect: "nonNull" })).shown).toEqual([
      "Items",
      "At a time",
      "Results",
    ]);
    // A result mapped to something other than the item itself is added.
    const mapped = loop({ body: { steps: [], output: { ref: "/item/id" } } });
    expect(shown(mapped).shown).toEqual(["Items", "Result per item"]);
  });

  it("reads the language's defaults from a new loop, each as not added", () => {
    const { subject, spec } = shown(loop());
    expect(readParam(subject, spec("concurrency"))).toEqual(fixed(1));
    expect(readParam(subject, spec("maxItems"))).toEqual(fixed(1000));
    expect(readParam(subject, spec("collect"))).toEqual(fixed("all"));
    expect(readParam(subject, spec("output"))).toEqual({
      mode: "mapped",
      expression: { ref: "/item" },
    });
    expect(spec("concurrency")).toMatchObject({ type: "number", min: 1 });
    expect(spec("maxItems")).toMatchObject({ type: "number", min: 1 });
    // The platform fails the run when the list holds more: nothing is left out.
    expect(spec("maxItems").hint).toBe(
      "The most items the list may hold. A longer list fails the run.",
    );
  });

  it("asks for a list, and offers lists first in the data picker", () => {
    const { subject, spec, env } = shown(loop());
    const items = spec("items");
    expect(items).toMatchObject({
      path: ["items"],
      type: "formula",
      mapping: "mapped",
      required: true,
      hint: "The list to run the steps for, one item at a time.",
    });
    expect(subject.schemaOf?.(["items"])).toEqual({ type: "array" });
    const option = (ref: string, schema: Record<string, unknown>) => ({
      ref,
      source: "input" as const,
      label: ref,
      breadcrumb: ref,
      typeLabel: "",
      optional: false,
      schema,
    });
    const view = referenceView(
      [
        option("/input/customer", { type: "object" }),
        option("/input/orders", { type: "array" }),
      ],
      "",
      subject.schemaOf?.(["items"]),
    );
    expect(view.flat.map((o) => [o.option.ref, o.fit])).toEqual([
      ["/input/orders", "compatible"],
      ["/input/customer", "incompatible"],
    ]);
    // Without a list, the field says what the loop needs.
    expect(
      fieldRules(
        items,
        readParam(env.subject, items),
        readEntries(env.subject, items),
      ),
    ).toMatchObject([
      { code: "required", message: "Choose the list to loop over." },
    ]);
  });

  it("writes every item's result, or only the ones that aren't empty", () => {
    const { subject, spec } = shown(loop());
    const results = spec("collect");
    expect(
      (results.choices as { value: unknown; label: string }[]).map((c) => [
        c.label,
        c.value,
      ]),
    ).toEqual([
      ["Keep every result", "all"],
      ["Skip empty results", "nonNull"],
    ]);
    const skipped = writeParam(subject, results, fixed("nonNull")).reduce(
      applyChange,
      subject,
    );
    expect(skipped.step!["collect"]).toBe("nonNull");
  });

  it("writes an option's default back when it is removed", () => {
    const step = loop({
      concurrency: 4,
      maxItems: 20,
      collect: "nonNull",
      body: { steps: [], output: { ref: "/item/id" } },
    });
    const { env, spec } = shown(step);
    const removed = ["concurrency", "maxItems", "collect", "output"]
      .flatMap((id) => resetParam(spec(id), env))
      .reduce(applyChange, env.subject);
    expect(removed.step).toMatchObject({
      concurrency: 1,
      maxItems: 1000,
      collect: "all",
    });
    // The item itself, mapped: never a fixed object that looks like a reference.
    expect((removed.step!["body"] as { output: unknown }).output).toEqual({
      ref: "/item",
    });
  });

  it("puts the compiler's loop problems on the fields they are about", () => {
    const { form } = shown(loop());
    const placed: PlacedSpec[] = [...form.fields, ...(form.options ?? [])].map(
      (spec) => ({ spec, tab: "parameters" }),
    );
    // Where the compiler reports them, under the step's own path.
    const at = (pointer: string) =>
      fieldFor(pointer.split("/").slice(1), placed)?.spec.label ?? null;
    expect(at("/items")).toBe("Items");
    expect(at("/concurrency")).toBe("At a time");
    expect(at("/maxItems")).toBe("Up to");
    expect(at("/body/output")).toBe("Result per item");
  });

  it("keeps On error on Stop the run, and says so", () => {
    expect(onErrorText("forEach")).toBe(
      "The run fails with this step's error. You can retry it from Operate.",
    );
    expect(ndvRegistry.kind("forEach")!.settings).toBeUndefined();
  });
});

describe("a loop in step details", () => {
  it("says what it loops over under its name", () => {
    const descriptor = ndvRegistry.kind("forEach")!;
    const step = loop({ items: { ref: "/input/orders" }, concurrency: 4 });
    expect(descriptor).toMatchObject({
      label: "Loop over items",
      feature: "flow.forEach",
      category: "flow",
      role: "loop",
      idPrefix: "loop",
    });
    expect(descriptor.summary(step, context(step))).toBe(
      "input.orders · up to 4 at a time",
    );
    expect(headerSubtitle(step, context(step), descriptor.label)).toBe(
      "Loop over items · input.orders · up to 4 at a time",
    );
    // One at a time, no suffix; without a list, the kind alone.
    const plain = loop({ items: { ref: "/input/orders" } });
    expect(descriptor.summary(plain, context(plain))).toBe("input.orders");
    expect(headerSubtitle(loop(), context(loop()), descriptor.label)).toBe(
      "Loop over items",
    );
  });

  it("finds the steps inside a loop, so step details can edit them", () => {
    const send = createStep("transform", "send");
    const step = loop({
      items: { ref: "/input/orders" },
      body: { steps: [send], output: { ref: "/item" } },
    });
    const ctx = context(step);
    expect(stepPath(ctx.workflow, "send")).toEqual([
      "spec",
      "steps",
      0,
      "body",
      "steps",
      0,
    ]);
    // Items and each item's result are the loop's expressions.
    expect(
      ndvRegistry
        .kind("forEach")!
        .fields(step)
        .map((f) => f.path),
    ).toEqual([["items"], ["body", "output"]]);
  });
});
