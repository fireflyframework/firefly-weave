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
// What a trigger says, on its tile and in Add a step: its name, icon, what
// it does, the words people search with, and the tile's subtitle. Triggers
// other than the input form are created when the workflow is activated.
import type { Workflow } from "../../model";
import type { CanvasSidecar } from "../state/canvas-sidecar";
import type { TriggerKind } from "./layout-ltr";
import { triggerSubtitle } from "./tile-facts";

export const TRIGGER_LABELS: Readonly<Record<TriggerKind, string>> = {
  manual: "Manual form",
  webhook: "Webhook",
  schedule: "Schedule",
  broker: "Kafka message",
  email: "Email",
  provider: "App event",
  called: "Called by a workflow",
};
export const TRIGGER_ICONS: Readonly<Record<TriggerKind, string>> = {
  manual: "manualTrigger",
  webhook: "webhook",
  schedule: "schedule",
  broker: "kafka",
  email: "email",
  provider: "appEvent",
  called: "calledTrigger",
};
export const TRIGGER_DESCRIPTIONS: Readonly<Record<TriggerKind, string>> = {
  manual: "Start runs yourself, from a form for the input",
  webhook: "Start a run when another system calls a URL",
  schedule: "Start runs on a timetable, in UTC",
  broker: "Start a run for each message on a topic",
  email: "Start a run when an email arrives",
  provider: "WhatsApp, Telegram or Teams events",
  called: "Run as a sub-workflow of other workflows",
};
export const TRIGGER_KEYWORDS: Readonly<Record<TriggerKind, string>> = {
  manual: "form manual start button input on demand",
  webhook: "http url post hook callback endpoint",
  schedule: "cron timer every daily weekly hourly interval time",
  broker: "kafka topic queue stream message broker",
  email: "mail inbox imap message",
  provider: "whatsapp telegram teams chat app message event",
  called: "sub-workflow subworkflow call execute workflow",
};
export const SET_UP_LATER = "Set up at activation";

const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);

/** A trigger tile's icon, title (the trigger's name) and a short subtitle that fits the tile. */
export function triggerTile(
  kind: TriggerKind,
  workflow: Workflow,
): { icon: string; title: string; subtitle: string } {
  const icon = TRIGGER_ICONS[kind];
  const title = TRIGGER_LABELS[kind];
  if (kind === "manual")
    return { icon, title, subtitle: triggerSubtitle(workflow) };
  if (kind === "called") {
    const callable = workflow.spec["callable"];
    const callers =
      isRecord(callable) && Array.isArray(callable["allowedCallers"])
        ? callable["allowedCallers"].length
        : 0;
    return {
      icon,
      title,
      subtitle: callers
        ? `${callers} allowed ${callers === 1 ? "caller" : "callers"}`
        : "Any workflow here",
    };
  }
  return { icon, title, subtitle: SET_UP_LATER };
}

/** Where a trigger tile's note is kept: the input form's under "$trigger", as step details writes it. */
export function triggerNoteKey(tileId: string, canvas: CanvasSidecar): string {
  const intent = canvas.triggers?.find(
    (item) => `$trigger:${item.id}` === tileId,
  );
  return !intent || intent.kind === "manual" ? "$trigger" : intent.id;
}
