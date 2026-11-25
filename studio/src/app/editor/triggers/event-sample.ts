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
// A pasted sample of a platform event: what is wrong with it, the first
// problem by field, and the event with its real IDs replaced. Test data is
// read by everyone who can test the workflow, so an event copied from a real
// transaction never keeps the IDs of its event, run or incident.
import { isJsonObject, type JsonObject } from "../../forms/core/json";
import type { Json } from "../ndv/registry";
import {
  EVENT_CHOICE_ORDER,
  EVENT_CHOICES,
  EVENT_TYPES,
  isPlatformEventType,
} from "./platform-event";

export const SAMPLE_FIELD_PROBLEM =
  "Paste a platform event: a JSON object with its type and transaction.";
/** Who reads a pasted event, and so what it must not hold. */
export const SHARED_NOTE =
  "Test data is shared with everyone who can test this workflow. Use a made-up business key.";
export type ReplacedId = "event" | "run" | "incident";

const VERSION = "weave/platform-event-v1";
const STATES = EVENT_CHOICE_ORDER.map((type) =>
  type.slice("transaction.".length),
);
const text = (value: unknown) => typeof value === "string" && value !== "";

/** The first field the platform would refuse, in its order; null when none. */
function problemOf(event: JsonObject): string | null {
  if (event["version"] !== VERSION) return `version must be ${VERSION}.`;
  const type = event["type"];
  if (!isPlatformEventType(type))
    return `type must be one of ${EVENT_TYPES.join(", ")}.`;
  const at = event["emitted_at"];
  if (typeof at !== "string" || Number.isNaN(Date.parse(at)))
    return "emitted_at must be a date and time.";
  const facts = event["transaction"];
  const group = (field: string) => `${field} must be a group of fields.`;
  if (!isJsonObject(facts)) return group("transaction");
  const workflow = facts["workflow"];
  if (!isJsonObject(workflow)) return group("transaction.workflow");
  if (!text(workflow["name"])) return "transaction.workflow.name must be text.";
  if (!text(workflow["version"]))
    return "transaction.workflow.version must be text.";
  if (!text(facts["instance_key"]))
    return "transaction.instance_key must be text.";
  if (facts["business_key"] !== null && !text(facts["business_key"]))
    return "transaction.business_key must be text or null.";
  const state = facts["state"];
  if (typeof state !== "string" || !STATES.includes(state))
    return `transaction.state must be one of ${STATES.join(", ")}.`;
  if (type !== `transaction.${state}`)
    return `transaction.state must be ${type.slice("transaction.".length)} for ${type}.`;
  if (!isJsonObject(facts["steps"])) return group("transaction.steps");
  if (typeof facts["test"] !== "boolean")
    return "transaction.test must be true or false.";
  return null;
}

/** The event pasted as `source` with sample IDs in place of its own, and which it replaced; or what is wrong with it. */
export function pastedEvent(
  source: string,
  id: () => string,
): { value: Json; replaced: ReplacedId[] } | { problem: string } {
  let event: unknown;
  try {
    event = JSON.parse(source);
  } catch {
    return { problem: SAMPLE_FIELD_PROBLEM };
  }
  if (!isJsonObject(event)) return { problem: SAMPLE_FIELD_PROBLEM };
  const problem = problemOf(event);
  if (problem) return { problem };
  const facts = event["transaction"] as JsonObject;
  const replaced: ReplacedId[] = [];
  const replace = (holder: JsonObject, key: string, name: ReplacedId) => {
    const held = holder[key] !== undefined && holder[key] !== null;
    if (held) replaced.push(name);
    // The event and the run always have one; an incident only some.
    if (held || name !== "incident") holder[key] = id();
  };
  replace(event, "event_id", "event");
  replace(facts, "run_id", "run");
  replace(facts, "incident_id", "incident");
  return { value: event as Json, replaced };
}

/** "Studio replaced the event and run IDs with sample IDs."; "" when it replaced none. */
export function replacedNote(replaced: readonly ReplacedId[]): string {
  if (!replaced.length) return "";
  if (replaced.length === 1)
    return `Studio replaced the ${replaced[0]} ID with a sample ID.`;
  const last = replaced[replaced.length - 1];
  return `Studio replaced the ${replaced.slice(0, -1).join(", ")} and ${last} IDs with sample IDs.`;
}

/** What a test event is, in a few words; null for data that isn't a platform event. */
export function eventSummary(
  value: Json | undefined,
): { label: string; text: string }[] | null {
  if (!isJsonObject(value)) return null;
  const facts = value["transaction"];
  if (!isPlatformEventType(value["type"]) || !isJsonObject(facts)) return null;
  const workflow = isJsonObject(facts["workflow"]) ? facts["workflow"] : {};
  const key = facts["business_key"];
  return [
    { label: "Event", text: EVENT_CHOICES[value["type"]].short },
    {
      label: "From",
      text: [workflow["name"], workflow["version"]].filter(Boolean).join(" "),
    },
    { label: "Business key", text: typeof key === "string" ? key : "None" },
  ];
}
