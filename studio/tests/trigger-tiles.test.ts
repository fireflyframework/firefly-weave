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
  it("say what starts the run, and that other triggers are set up when you activate", () => {
    const workflow = freshWorkflow();
    workflow.spec["inputSchema"] = {
      type: "object",
      properties: { amount: { type: "number" } },
    };
    expect(triggerTile("manual", workflow)).toEqual({
      icon: "manualTrigger",
      subtitle: "Manual form · 1 field",
    });
    expect(triggerTile("webhook", workflow)).toEqual({
      icon: "webhook",
      subtitle: "Webhook · Set up when you activate",
    });
    expect(triggerTile("broker", workflow).subtitle).toBe(
      "Kafka message · Set up when you activate",
    );
    expect(triggerTile("provider", workflow).subtitle).toBe(
      "App event · Set up when you activate",
    );
    expect(triggerTile("called", workflow).subtitle).toBe(
      "Called by a workflow · Any workflow in this project",
    );
    workflow.spec["callable"] = { allowedCallers: ["order-intake", "billing"] };
    expect(triggerTile("called", workflow).subtitle).toBe(
      "Called by a workflow · 2 allowed callers",
    );
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
