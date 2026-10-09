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
  NDV_REGISTRY_VERSION,
  NdvRegistry,
  RegistryError,
  ndvRegistry,
  registerKind,
  type NdvContext,
  type ParameterRegistration,
  type StepKindDescriptor,
  type SubNodeSlotSpec,
} from "../src/app/editor/ndv/registry";
import { createStep, type Step } from "../src/app/model";

const probe = (kind: string, idPrefix = "probe"): StepKindDescriptor => ({
  kind,
  label: "Probe",
  description: "A kind for this test",
  keywords: "probe",
  icon: "action",
  role: "app",
  category: "app",
  idPrefix,
  create: (id) => createStep("transform", id),
  fields: () => [],
  summary: () => "",
  outputSchema: () => null,
  pinnable: false,
});
const parameters = (kind: string, subNode?: string): ParameterRegistration => ({
  kind,
  subNode,
  load: () => Promise.reject(new Error("Not loaded in this test")),
});
const slot = (id: string, max: number | null = 1): SubNodeSlotSpec => ({
  id,
  label: id,
  required: false,
  max,
  chip: () => ({ text: id, state: "ok" }),
  entries: async () => [],
});

describe("step details registry", () => {
  it("is version 2", () => {
    expect(NDV_REGISTRY_VERSION).toBe(2);
    expect(new NdvRegistry().version).toBe(2);
  });

  it("keeps kinds in registration order", () => {
    const registry = new NdvRegistry();
    registry.registerKind(probe("first"));
    registry.registerKind(probe("second"));
    expect(registry.kinds().map((d) => d.kind)).toEqual(["first", "second"]);
    expect(registry.kind("second")?.label).toBe("Probe");
    expect(registry.kind("missing")).toBeUndefined();
  });

  it("refuses a kind registered twice", () => {
    const registry = new NdvRegistry();
    registry.registerKind(probe("first"));
    expect(() => registry.registerKind(probe("first"))).toThrow(
      new RegistryError('Step kind "first" is already registered.'),
    );
  });

  it("refuses an ID prefix that can't start a step ID", () => {
    expect(() => new NdvRegistry().registerKind(probe("odd", "-odd"))).toThrow(
      RegistryError,
    );
  });

  it("keeps parameter components per kind and per AI agent slot", () => {
    const registry = new NdvRegistry();
    const agent = parameters("agent");
    const tools = parameters("agent", "tools");
    registry.registerParameters(agent);
    registry.registerParameters(tools);
    expect(registry.parameters("agent")).toBe(agent);
    expect(registry.parameters("agent", "tools")).toBe(tools);
    expect(() =>
      registry.registerParameters(parameters("agent", "tools")),
    ).toThrow(
      new RegistryError('Parameters for "agent/tools" are already registered.'),
    );
  });

  it("keeps AI agent slots in order and checks them", () => {
    const registry = new NdvRegistry();
    registry.registerSubNodes("agent", [slot("model"), slot("tools", null)]);
    expect(registry.subNodes("agent").map((s) => s.id)).toEqual([
      "model",
      "tools",
    ]);
    expect(registry.subNodes("transform")).toEqual([]);
    expect(() => registry.registerSubNodes("agent", [slot("memory")])).toThrow(
      RegistryError,
    );
    expect(() =>
      new NdvRegistry().registerSubNodes("agent", [
        slot("tools"),
        slot("tools"),
      ]),
    ).toThrow(new RegistryError('Slot "tools" is listed twice for "agent".'));
    expect(() =>
      new NdvRegistry().registerSubNodes("agent", [slot("tools", 0)]),
    ).toThrow(RegistryError);
  });

  it("keeps how a kind runs in an environment, without the draft identity", () => {
    const registry = new NdvRegistry();
    registry.registerKind({
      ...probe("runnable"),
      real: {
        request: (ctx) =>
          ctx.step["uses"]
            ? {
                body: {
                  kind: ctx.step.kind,
                  step_id: ctx.step.id,
                  uses: String(ctx.step["uses"]),
                  input: { id: "c-1" },
                  timeout_seconds: 120,
                },
              }
            : { blocked: "Choose an action first." },
      },
    });
    const real = registry.kind("runnable")?.real;
    const at = (step: Step) => ({ step }) as unknown as NdvContext;
    const step = { ...createStep("transform", "check"), uses: "crm.get@1.0.0" };

    expect(real?.request(at(createStep("transform", "check")))).toEqual({
      blocked: "Choose an action first.",
    });
    const answer = real?.request(at(step));
    if (!answer || !("body" in answer)) throw new Error("Expected a body");
    expect(Object.keys(answer.body).sort()).toEqual([
      "input",
      "kind",
      "step_id",
      "timeout_seconds",
      "uses",
    ]);
    // The editor adds the draft's ID and revision when it sends the request.
    expect({
      ...answer.body,
      draft_id: "0f8f2a10-3c4d-4e5f-8a9b-1c2d3e4f5a6b",
      draft_revision: 12,
    }).toMatchObject({ step_id: "check", draft_revision: 12 });
  });

  it("registers through the shared functions into the shared registry", () => {
    registerKind(probe("shared-probe"));
    expect(ndvRegistry.kind("shared-probe")?.kind).toBe("shared-probe");
  });
});
