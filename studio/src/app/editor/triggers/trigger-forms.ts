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
// The settings of each trigger in step details, as forms the shared
// renderer draws: paths are relative to the trigger choice in the canvas
// sidecar, except the workflow's input schema (scope "workflow"), which a
// webhook's and a Kafka message's payload is. Also what an unfinished
// trigger still needs, the lines its step details show, and the question
// asked when a trigger's payload doesn't fit the workflow's input.
import { absoluteTime } from "../../format";
import {
  isJsonObject,
  jsonEqual,
  type JsonObject,
} from "../../forms/core/json";
import type { FormSpec, ParamSpec } from "../ndv/registry";
import type { TriggerIntent, TriggerKind } from "../state/canvas-sidecar";
import {
  describeCalendar,
  nextOccurrences,
  readPreset,
  validateCalendar,
} from "./schedule";

/** The largest webhook body the platform accepts, in bytes (1 MiB). */
export const MAX_BODY_BYTES = 1048576;
/** The largest timestamp tolerance the platform accepts, in seconds. */
export const MAX_TOLERANCE_SECONDS = 300;

const NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;
const TOPIC = /^[a-zA-Z0-9_-][a-zA-Z0-9._-]*$/;
const MAX_TOPIC = 249;

const NAME_RULE =
  "Use letters, numbers, dots, underscores or hyphens, starting with a letter or number.";
const SECRET_NEEDED = "Choose the secret that signs requests.";
const CRON_NEEDED = "Choose when the schedule runs.";
const TRIGGER_NAME_NEEDED = "Name the trigger.";
const CLUSTER_NEEDED =
  "Name the cluster, as configured on the Kafka connection.";
const TOPIC_RULE =
  "Use a topic name with letters, numbers, dots, underscores or hyphens (at most 249).";
const POLICY_NEEDED = "Choose what happens when a message can't start a run.";
const APP_NEEDED = "Choose the app and event.";
const NO_MAILBOX = "Choose the email connection when you activate.";

const inputSchema = (id: string, label: string, hint: string): ParamSpec => ({
  id,
  path: ["spec", "inputSchema"],
  scope: "workflow",
  type: "schema",
  label,
  default: { type: "object" },
  whenRemoved: "default",
  hint,
});

const notice = (id: string, label: string, hint: string): ParamSpec => ({
  id,
  path: [],
  type: "notice",
  label,
  hint,
});

const triggerName = (label: string, requiredText: string): ParamSpec => ({
  id: "name",
  path: ["name"],
  type: "text",
  label,
  required: true,
  identifier: true,
  requiredText,
  hint: "Letters, numbers, dots, underscores or hyphens; unique in the environment.",
});

const webhookForm = (): FormSpec => ({
  fields: [
    { ...triggerName("Name", "Name the webhook."), placeholder: "orders-in" },
    {
      id: "secretRef",
      path: ["secretRef"],
      type: "text",
      label: "Secret",
      required: true,
      identifier: true,
      placeholder: "orders-signing",
      requiredText: SECRET_NEEDED,
      hint: "The platform secret that signs requests. Studio never sees its value; create it with weave platform secret.",
    },
    inputSchema(
      "payload",
      "Expected payload",
      "What a request sends; it is the workflow's input.",
    ),
  ],
  options: [
    {
      id: "maxBodyBytes",
      path: ["maxBodyBytes"],
      type: "number",
      label: "Maximum body size",
      default: MAX_BODY_BYTES,
      min: 1,
      max: MAX_BODY_BYTES,
      hint: "In bytes, from 1 byte to 1 MiB.",
    },
    {
      id: "toleranceSeconds",
      path: ["toleranceSeconds"],
      type: "number",
      label: "Timestamp tolerance",
      default: MAX_TOLERANCE_SECONDS,
      min: 1,
      max: MAX_TOLERANCE_SECONDS,
      hint: "How far a request's signed time may be from now, 1 to 300 seconds.",
    },
  ],
});

const scheduleForm = (): FormSpec => ({
  fields: [
    {
      id: "cron",
      path: ["cron"],
      type: "custom",
      component: "schedule-builder",
      label: "When it runs",
      required: true,
      requiredText: CRON_NEEDED,
      hint: "In UTC. A time missed while the platform was down is skipped.",
    },
    {
      id: "input",
      path: ["input"],
      type: "keyValue",
      mapping: "fixed",
      label: "Run input",
      default: {},
      whenRemoved: "default",
      hint: "What each run starts with; it must fit the workflow's input fields.",
    },
  ],
});

const kafkaForm = (): FormSpec => ({
  fields: [
    {
      ...triggerName("Trigger name", TRIGGER_NAME_NEEDED),
      placeholder: "orders",
    },
    {
      id: "clusterId",
      path: ["clusterId"],
      type: "text",
      label: "Cluster",
      required: true,
      identifier: true,
      requiredText: CLUSTER_NEEDED,
      hint: "The cluster name the operator gave the Kafka connection.",
    },
    {
      id: "topic",
      path: ["topic"],
      type: "text",
      label: "Topic",
      required: true,
      maxLength: MAX_TOPIC,
      placeholder: "orders.created",
      requiredText: TOPIC_RULE,
    },
    {
      id: "deadLetterPolicy",
      path: ["deadLetterPolicy"],
      type: "select",
      label: "When a message can't start a run",
      required: true,
      requiredText: POLICY_NEEDED,
      choices: [
        {
          value: "receipt",
          label: "Record it and continue",
          description: "The offset moves on.",
        },
        {
          value: "halt",
          label: "Stop the trigger until someone fixes it",
          description: "The route is blocked and an incident opens.",
        },
      ],
    },
    inputSchema(
      "payload",
      "Message payload",
      "What a message holds; it is the workflow's input.",
    ),
  ],
});

const emailForm = (): FormSpec => ({
  fields: [
    {
      ...inputSchema("input", "Run input", ""),
      hint: undefined,
      readOnly: () => "The run input is the email message.",
    },
    notice("mailbox", "Mailbox", NO_MAILBOX),
    notice(
      "attachments",
      "Attachments",
      "Attachments arrive as metadata; their content is not in the run input.",
    ),
    notice(
      "arrivals",
      "New mail only",
      "Only mail that arrives after the source starts begins runs.",
    ),
    notice(
      "automated",
      "Automatic replies",
      "Automatic replies have automated: true; branch on it to avoid reply loops.",
    ),
  ],
});

const appEventForm = (): FormSpec => ({
  fields: [
    {
      ...triggerName("Trigger name", TRIGGER_NAME_NEEDED),
      placeholder: "support",
    },
    {
      id: "provider",
      path: ["provider"],
      type: "resource",
      label: "App and event",
      required: true,
      requiredText: APP_NEEDED,
      hint: "The app and the event that starts a run.",
    },
    {
      id: "mapping",
      path: ["mapping"],
      type: "formula",
      label: "Run input",
      default: { ref: "/payload" },
      whenRemoved: "default",
      hint: "What each run starts with, from the event; /payload is the event's data.",
    },
  ],
});

/** The settings form of a trigger's step details; a manual form and a called workflow have their own. */
export function triggerForm(kind: TriggerKind): FormSpec {
  switch (kind) {
    case "webhook":
      return webhookForm();
    case "schedule":
      return scheduleForm();
    case "broker":
      return kafkaForm();
    case "email":
      return emailForm();
    case "provider":
      return appEventForm();
    case "manual":
    case "called":
      return { fields: [] };
  }
}

const text = (intent: TriggerIntent, key: string) => {
  const value = intent[key];
  return typeof value === "string" && value.trim() ? value : undefined;
};

const named = (
  problems: Record<string, string>,
  key: string,
  value: string | undefined,
  needed: string,
) => {
  if (value === undefined) problems[key] = needed;
  else if (!NAME.test(value)) problems[key] = NAME_RULE;
};

const within = (value: unknown, low: number, high: number) =>
  value === undefined ||
  (typeof value === "number" &&
    Number.isInteger(value) &&
    value >= low &&
    value <= high);

/** What a trigger choice still needs before it can be activated, by field ID; empty when it is ready. */
export function triggerProblems(intent: TriggerIntent): Record<string, string> {
  const problems: Record<string, string> = {};
  switch (intent.kind) {
    case "webhook":
      named(problems, "name", text(intent, "name"), "Name the webhook.");
      named(problems, "secretRef", text(intent, "secretRef"), SECRET_NEEDED);
      if (!within(intent["maxBodyBytes"], 1, MAX_BODY_BYTES))
        problems["maxBodyBytes"] =
          "Use a size from 1 byte to 1 MiB (1,048,576 bytes).";
      if (!within(intent["toleranceSeconds"], 1, MAX_TOLERANCE_SECONDS))
        problems["toleranceSeconds"] = "Use a tolerance from 1 to 300 seconds.";
      break;
    case "schedule": {
      const cron = text(intent, "cron");
      const problem = cron === undefined ? CRON_NEEDED : validateCalendar(cron);
      if (problem) problems["cron"] = problem;
      break;
    }
    case "broker": {
      named(problems, "name", text(intent, "name"), TRIGGER_NAME_NEEDED);
      const cluster = text(intent, "clusterId");
      if (cluster === undefined || !NAME.test(cluster))
        problems["clusterId"] = CLUSTER_NEEDED;
      const topic = text(intent, "topic");
      if (topic === undefined || topic.length > MAX_TOPIC || !TOPIC.test(topic))
        problems["topic"] = TOPIC_RULE;
      const policy = intent["deadLetterPolicy"];
      if (policy !== "receipt" && policy !== "halt")
        problems["deadLetterPolicy"] = POLICY_NEEDED;
      break;
    }
    case "provider":
      named(problems, "name", text(intent, "name"), TRIGGER_NAME_NEEDED);
      if (
        ["provider", "package", "packageVersion", "schemaDigest"].some(
          (key) => text(intent, key) === undefined,
        )
      )
        problems["provider"] = APP_NEEDED;
      break;
    case "email":
    case "manual":
    case "called":
      break;
  }
  return problems;
}

export type SchedulePreview =
  | { name: string; next: string[]; note?: string }
  | { problem: string };

/** A schedule's name and the next five times it runs, in UTC, or why the platform would refuse it. */
export function schedulePreview(cron: string, now: Date): SchedulePreview {
  const problem = validateCalendar(cron);
  if (problem) return { problem };
  const preset = readPreset(cron);
  const preview: SchedulePreview = {
    name: describeCalendar(cron),
    next: nextOccurrences(cron, now, 5).map(
      (moment) => `${absoluteTime(moment, "UTC")} UTC`,
    ),
  };
  if (preset.kind === "monthly" && preset.day > 28)
    preview.note = `Months without day ${preset.day} are skipped.`;
  return preview;
}

/** The mailbox line of an email trigger: the email connection bound here, or when it is chosen. */
export function mailboxLine(
  connection: { name: string; mailbox: string } | undefined,
): string {
  return connection ? `${connection.name} · ${connection.mailbox}` : NO_MAILBOX;
}

/** Why a Kafka connection can't serve a trigger's cluster, or null when it does. */
export function kafkaClusterMismatch(
  connection: string,
  served: string,
  cluster: string,
): string | null {
  return served === cluster
    ? null
    : `${connection} serves cluster ${served}, not ${cluster}.`;
}

export type InputFit =
  | { kind: "fits" }
  | { kind: "replace"; schema: JsonObject }
  | {
      kind: "ask";
      question: string;
      replace: string;
      cancel: string;
      schema: JsonObject;
    }
  | { kind: "problem"; message: string; schema: JsonObject };

/** An input that asks for nothing yet: a new workflow's input. */
function blank(schema: JsonObject) {
  const { type, properties, ...rest } = schema;
  return (
    !Object.keys(rest).length &&
    (type === undefined || type === "object") &&
    (properties === undefined ||
      (isJsonObject(properties) && !Object.keys(properties).length))
  );
}

/**
 * Whether a trigger's payload fits the workflow's one input. Only an email
 * trigger brings its own payload (the email message); a webhook's and a
 * Kafka message's payload is the input itself. Adding an email trigger to a
 * workflow whose input asks for something else asks before replacing it; an
 * email trigger already there reports the misfit instead.
 */
export function inputFit(
  kind: TriggerKind,
  input: JsonObject,
  emailSchema: JsonObject,
  options: { existing?: boolean } = {},
): InputFit {
  if (kind !== "email" || jsonEqual(input, emailSchema))
    return { kind: "fits" };
  if (options.existing)
    return {
      kind: "problem",
      message:
        "This workflow's input no longer accepts an email. Reset the input to the email message.",
      schema: emailSchema,
    };
  if (blank(input)) return { kind: "replace", schema: emailSchema };
  return {
    kind: "ask",
    question:
      "The email message doesn't fit this workflow's input. Replace the input with the email message?",
    replace: "Replace",
    cancel: "Cancel",
    schema: emailSchema,
  };
}
