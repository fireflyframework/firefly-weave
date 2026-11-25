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
/** A test URL never shares the production handle: a signature doesn't cover the route. */
export const TEST_SECRET_REUSED =
  "Use a test secret other than the production secret.";
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
    {
      id: "testSecretRef",
      path: ["testSecretRef"],
      type: "text",
      label: "Test secret",
      identifier: true,
      placeholder: "orders-test-signing",
      hint: "A secret of its own that signs test events, never the production one, so a production event can't start a test run. Create it with weave platform secret.",
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

/** A schedule's calendar is set with its builder (above the fields); the form holds the run input. */
const scheduleForm = (): FormSpec => ({
  fields: [
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
      "Automatic replies have `automated: true`; branch on it to avoid reply loops.",
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
      // An example a builder recognizes: an app, and an event it sends.
      placeholder: "whatsapp · message received",
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

/** The settings form of a trigger's step details; a manual form, a called workflow and a platform event have their own. */
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
    case "event":
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
      {
        const test = text(intent, "testSecretRef");
        if (test !== undefined && !NAME.test(test))
          problems["testSecretRef"] = NAME_RULE;
        else if (test !== undefined && test === text(intent, "secretRef"))
          problems["testSecretRef"] = TEST_SECRET_REUSED;
      }
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
    // The definition holds a platform event's settings, not the choice.
    case "event":
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

/** The JSON types a schema names, or null when it names none (any value). */
function typesOf(schema: unknown): Set<string> | null {
  if (!isJsonObject(schema)) return null;
  const type = schema["type"];
  if (typeof type === "string") return new Set([type]);
  if (Array.isArray(type) && type.every((item) => typeof item === "string"))
    return new Set(type as string[]);
  return null;
}

/** Whether a value of one of `given`'s types can be one of `wanted`'s (an integer is a number). */
const overlaps = (wanted: Set<string>, given: Set<string>) =>
  [...given].some(
    (type) => wanted.has(type) || (type === "integer" && wanted.has("number")),
  );

/**
 * Whether the input asks for nothing the email message lacks: an object,
 * required fields the message has, no message field of another type, and
 * room for every message field. Finer checks are the platform's (it accepts
 * any input compatible with the message when the email source moves).
 */
function takesEmail(input: JsonObject, message: JsonObject): boolean {
  const kinds = typesOf(input);
  if (kinds && !kinds.has("object")) return false;
  const fields = isJsonObject(message["properties"])
    ? message["properties"]
    : {};
  const declared = isJsonObject(input["properties"]) ? input["properties"] : {};
  const required = Array.isArray(input["required"]) ? input["required"] : [];
  if (required.some((name) => typeof name !== "string" || !(name in fields)))
    return false;
  for (const [name, field] of Object.entries(declared)) {
    if (!(name in fields)) continue;
    const wanted = typesOf(field);
    const given = typesOf(fields[name]);
    if (wanted && given && !overlaps(wanted, given)) return false;
  }
  return !(
    input["additionalProperties"] === false &&
    Object.keys(fields).some((name) => !(name in declared))
  );
}

/**
 * Whether a trigger's payload fits the workflow's one input. Only an email
 * trigger brings its own payload: the received message, whose schema
 * (`emailSchema`) is the platform's NormalizedEmail with its definitions
 * inlined, not the exported email-message envelope. A webhook's and a Kafka
 * message's payload is the input itself. Adding an email trigger to a
 * workflow whose input asks for something the message lacks asks before
 * replacing it; an email trigger already there reports the misfit instead.
 */
export function inputFit(
  kind: TriggerKind,
  input: JsonObject,
  emailSchema: JsonObject,
  options: { existing?: boolean } = {},
): InputFit {
  if (kind !== "email" || jsonEqual(input, emailSchema))
    return { kind: "fits" };
  if (!options.existing && blank(input))
    return { kind: "replace", schema: emailSchema };
  if (takesEmail(input, emailSchema)) return { kind: "fits" };
  if (options.existing)
    return {
      kind: "problem",
      message:
        "This workflow's input no longer accepts an email. Reset the input to the email message.",
      schema: emailSchema,
    };
  return {
    kind: "ask",
    question:
      "The email message doesn't fit this workflow's input. Replace the input with the email message?",
    replace: "Replace",
    cancel: "Cancel",
    schema: emailSchema,
  };
}
