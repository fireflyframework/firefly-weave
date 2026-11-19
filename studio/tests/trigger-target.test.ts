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
// Step details targets for triggers: the input form ("$trigger") and each
// trigger choice ("$trigger:<id>") start the workflow, so they come first in
// the outline and say so in their settings.
import { describe, expect, it } from "vitest";
import { outlineRank } from "../src/app/editor/ndv/navigation";
import { placementText } from "../src/app/editor/ndv/settings/settings-rules";
import {
  isTriggerChoice,
  isTriggerTarget,
  triggerChoiceOf,
  triggerChoiceTarget,
} from "../src/app/editor/triggers/trigger-target";
import type { CanvasSidecar } from "../src/app/editor/state/canvas-sidecar";
import type { Workflow } from "../src/app/model";

const WORKFLOW = {
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name: "orders", version: "1.0.0" },
  spec: {
    inputSchema: { type: "object" },
    steps: [
      { id: "check", kind: "transform", value: { ref: "/input" } },
      { id: "send", kind: "transform", value: { literal: {} } },
    ],
  },
} as unknown as Workflow;
const CANVAS = {
  schemaVersion: 2,
  kind: "weave.studio/canvas",
  triggers: [
    { id: "trigger-0a1b2c3d", kind: "manual" },
    { id: "trigger-1b2c3d4e", kind: "webhook", name: "orders-in" },
  ],
} as unknown as CanvasSidecar;

describe("trigger targets", () => {
  it("names a trigger choice as its tile is named, and finds it in the canvas", () => {
    const target = triggerChoiceTarget("trigger-1b2c3d4e");
    expect(target).toBe("$trigger:trigger-1b2c3d4e");
    expect([
      isTriggerChoice(target),
      isTriggerTarget(target),
      isTriggerTarget("$trigger"),
    ]).toEqual([true, true, true]);
    expect([
      isTriggerChoice("$trigger"),
      isTriggerTarget("$end"),
      isTriggerTarget("check"),
    ]).toEqual([false, false, false]);
    expect(triggerChoiceOf(CANVAS, target)).toEqual({
      id: "trigger-1b2c3d4e",
      kind: "webhook",
      name: "orders-in",
    });
    expect(triggerChoiceOf(CANVAS, "$trigger:trigger-99999999")).toBeNull();
    expect(triggerChoiceOf(CANVAS, "check")).toBeNull();
  });

  it("puts a trigger choice first in the outline and says it starts the workflow", () => {
    const rank = outlineRank(WORKFLOW);
    const choice = triggerChoiceTarget("trigger-1b2c3d4e");
    expect([
      rank(choice),
      rank("$trigger"),
      rank("check"),
      rank("send"),
      rank("$end"),
    ]).toEqual([-1, -1, 0, 1, 2]);
    expect(placementText(WORKFLOW, choice)).toBe("Starts the workflow");
    expect(placementText(WORKFLOW, "$end")).toBe("");
  });
});
