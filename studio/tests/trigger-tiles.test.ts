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
import {
  activatedAsIs,
  settingsKey,
} from "../src/app/editor/triggers/trigger-activated";
import type { TriggerIntent } from "../src/app/editor/state/canvas-sidecar";
import { describe, expect, it } from "vitest";
import { isIconName } from "../src/app/icon";
import { freshWorkflow } from "../src/app/model";
import {
  TRIGGER_KINDS,
  emptyCanvas,
} from "../src/app/editor/state/canvas-sidecar";
import {
  TRIGGER_DESCRIPTIONS,
  TRIGGER_ICONS,
  TRIGGER_KEYWORDS,
  TRIGGER_LABELS,
  triggerNoteKey,
  triggerTile,
} from "../src/app/editor/canvas/trigger-tiles";

describe("trigger tiles", () => {
  it("are titled by the trigger, and say what starts the run or that it is set up when you activate", () => {
    const workflow = freshWorkflow();
    workflow.spec["inputSchema"] = {
      type: "object",
      properties: { amount: { type: "number" } },
    };
    expect(triggerTile("manual", workflow)).toEqual({
      icon: "manualTrigger",
      title: "Manual form",
      subtitle: "1 field",
    });
    expect(triggerTile("webhook", workflow)).toEqual({
      icon: "webhook",
      title: "Webhook",
      subtitle: "Set up at activation",
    });
    expect(triggerTile("broker", workflow)).toMatchObject({
      title: "Kafka message",
      subtitle: "Set up at activation",
    });
    expect(triggerTile("provider", workflow).title).toBe("App event");
    expect(triggerTile("called", workflow)).toMatchObject({
      title: "Called by a workflow",
      subtitle: "Any workflow",
    });
    workflow.spec["callable"] = { allowedCallers: ["order-intake", "billing"] };
    expect(triggerTile("called", workflow).subtitle).toBe("Only 2 workflows");
    workflow.spec["callable"] = { allowedCallers: ["order-intake"] };
    expect(triggerTile("called", workflow).subtitle).toBe("Only 1 workflow");
  });

  it("says a trigger is set up at activation until it is activated with the settings it has now", () => {
    const workflow = freshWorkflow();
    // The canvas passes the choice only once an activation took its settings.
    const settled = (intent: TriggerIntent) =>
      activatedAsIs(intent) ? intent : undefined;
    const hook = {
      id: "trigger-1b2c3d4e",
      kind: "webhook" as const,
      name: "orders-in",
      secretRef: "signing",
    };
    expect(triggerTile("webhook", workflow, settled(hook)).subtitle).toBe(
      "Set up at activation",
    );
    const activated = {
      ...hook,
      resources: { env: "w-1" },
      activated: { env: settingsKey(hook) },
    };
    expect(triggerTile("webhook", workflow, settled(activated)).subtitle).toBe(
      "orders-in",
    );
    expect(
      triggerTile(
        "webhook",
        workflow,
        settled({ ...activated, name: "orders-new" }),
      ).subtitle,
    ).toBe("Set up at activation");
    const schedule = {
      id: "trigger-2c3d4e5f",
      kind: "schedule" as const,
      cron: "0 9 * * 1",
    };
    expect(
      triggerTile(
        "schedule",
        workflow,
        settled({ ...schedule, activated: { env: settingsKey(schedule) } }),
      ).subtitle,
    ).toBe("0 9 * * 1 UTC");
  });

  it("names and describes every trigger, with an icon Studio draws", () => {
    for (const kind of TRIGGER_KINDS) {
      expect(TRIGGER_LABELS[kind], kind).toBeTruthy();
      expect(TRIGGER_DESCRIPTIONS[kind], kind).toBeTruthy();
      expect(TRIGGER_KEYWORDS[kind], kind).toBeTruthy();
      expect(isIconName(TRIGGER_ICONS[kind]), kind).toBe(true);
    }
  });

  it("keeps the input form's note where step details writes it, and other triggers' under their own ID", () => {
    const canvas = {
      ...emptyCanvas(),
      triggers: [
        { id: "trigger-0000000a", kind: "manual" as const },
        { id: "trigger-0000000b", kind: "webhook" as const },
      ],
    };
    expect(triggerNoteKey("$trigger:manual", emptyCanvas())).toBe("$trigger");
    expect(triggerNoteKey("$trigger:trigger-0000000a", canvas)).toBe(
      "$trigger",
    );
    expect(triggerNoteKey("$trigger:trigger-0000000b", canvas)).toBe(
      "trigger-0000000b",
    );
  });
});
