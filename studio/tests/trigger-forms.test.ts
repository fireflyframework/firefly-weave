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
// Trigger settings in step details: the fields of each trigger's form, their
// defaults, what an unfinished trigger still needs, and the question asked
// when a trigger's payload doesn't fit the workflow's input.
import { describe, expect, it } from "vitest";
import type { ParamSpec } from "../src/app/editor/ndv/registry";
import {
  inputFit,
  kafkaClusterMismatch,
  mailboxLine,
  schedulePreview,
  triggerForm,
  triggerProblems,
} from "../src/app/editor/triggers/trigger-forms";

const shape = (fields: ParamSpec[]) =>
  fields.map((field) => ({
    id: field.id,
    path: field.path,
    scope: field.scope ?? "step",
    type: field.type,
    label: field.label,
    required: field.required ?? false,
    default: field.default,
  }));

describe("trigger forms", () => {
  it("asks a webhook for its name, secret, body size and tolerance, over the workflow's input", () => {
    const form = triggerForm("webhook");
    expect(shape(form.fields)).toEqual([
      {
        id: "name",
        path: ["name"],
        scope: "step",
        type: "text",
        label: "Name",
        required: true,
        default: undefined,
      },
      {
        id: "secretRef",
        path: ["secretRef"],
        scope: "step",
        type: "text",
        label: "Secret",
        required: true,
        default: undefined,
      },
      {
        id: "testSecretRef",
        path: ["testSecretRef"],
        scope: "step",
        type: "text",
        label: "Test secret",
        required: false,
        default: undefined,
      },
      {
        id: "payload",
        path: ["spec", "inputSchema"],
        scope: "workflow",
        type: "schema",
        label: "Expected payload",
        required: false,
        default: { type: "object" },
      },
    ]);
    expect(shape(form.options ?? [])).toEqual([
      {
        id: "maxBodyBytes",
        path: ["maxBodyBytes"],
        scope: "step",
        type: "number",
        label: "Maximum body size",
        required: false,
        default: 1048576,
      },
      {
        id: "toleranceSeconds",
        path: ["toleranceSeconds"],
        scope: "step",
        type: "number",
        label: "Timestamp tolerance",
        required: false,
        default: 300,
      },
    ]);
    const [size, tolerance] = form.options ?? [];
    expect([size.min, size.max, tolerance.min, tolerance.max]).toEqual([
      1, 1048576, 1, 300,
    ]);
    expect(form.fields[1].hint).toBe(
      "The platform secret that signs requests. Studio never sees its value; create it with weave platform secret.",
    );
    expect([form.fields[2].identifier, form.fields[2].hint]).toEqual([
      true,
      "A secret of its own that signs test events, never the production one, so a production event can't start a test run. Create it with weave platform secret.",
    ]);
  });

  it("asks a schedule when it runs and the run input it sends", () => {
    const form = triggerForm("schedule");
    expect(shape(form.fields)).toEqual([
      {
        id: "cron",
        path: ["cron"],
        scope: "step",
        type: "custom",
        label: "When it runs",
        required: true,
        default: undefined,
      },
      {
        id: "input",
        path: ["input"],
        scope: "step",
        type: "keyValue",
        label: "Run input",
        required: false,
        default: {},
      },
    ]);
    expect(form.fields[1].mapping).toBe("fixed");
    expect(form.fields[0].component).toBe("schedule-builder");
  });

  it("asks a Kafka trigger for its name, cluster, topic and what to do with a message that can't start a run", () => {
    const form = triggerForm("broker");
    expect(
      form.fields.map((field) => [
        field.id,
        field.label,
        field.required ?? false,
      ]),
    ).toEqual([
      ["name", "Trigger name", true],
      ["clusterId", "Cluster", true],
      ["topic", "Topic", true],
      ["deadLetterPolicy", "When a message can't start a run", true],
      ["payload", "Message payload", false],
    ]);
    const policy = form.fields[3];
    expect(policy.choices).toEqual([
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
    ]);
    expect(form.fields[2].maxLength).toBe(249);
  });

  it("shows an email trigger's run input read only, with its mailbox and hints", () => {
    const form = triggerForm("email");
    expect(
      form.fields.map((field) => [field.id, field.type, field.label]),
    ).toEqual([
      ["input", "schema", "Run input"],
      ["mailbox", "notice", "Mailbox"],
      ["attachments", "notice", "Attachments"],
      ["arrivals", "notice", "New mail only"],
      ["automated", "notice", "Automatic replies"],
    ]);
    // A notice shows its hint; a read-only field shows its reason there.
    expect(form.fields.slice(1).map((field) => field.hint)).toEqual([
      "Choose the email connection when you activate.",
      "Attachments arrive as metadata; their content is not in the run input.",
      "Only mail that arrives after the source starts begins runs.",
      "Automatic replies have automated: true; branch on it to avoid reply loops.",
    ]);
    const input = form.fields[0];
    expect([input.scope, input.path]).toEqual([
      "workflow",
      ["spec", "inputSchema"],
    ]);
    expect(input.readOnly?.({} as never, {} as never)).toBe(
      "The run input is the email message.",
    );
  });

  it("asks an app event trigger for its app and how the event maps to the input", () => {
    const form = triggerForm("provider");
    expect(shape(form.fields)).toEqual([
      {
        id: "name",
        path: ["name"],
        scope: "step",
        type: "text",
        label: "Trigger name",
        required: true,
        default: undefined,
      },
      {
        id: "provider",
        path: ["provider"],
        scope: "step",
        type: "resource",
        label: "App and event",
        required: true,
        default: undefined,
      },
      {
        id: "mapping",
        path: ["mapping"],
        scope: "step",
        type: "formula",
        label: "Run input",
        required: false,
        default: { ref: "/payload" },
      },
    ]);
  });

  it("has no settings for a manual form or a called workflow here", () => {
    expect(triggerForm("manual")).toEqual({ fields: [] });
    expect(triggerForm("called")).toEqual({ fields: [] });
  });
});

describe("what a trigger still needs", () => {
  it("names each missing or invalid setting of a webhook", () => {
    expect(triggerProblems({ id: "t", kind: "webhook" })).toEqual({
      name: "Name the webhook.",
      secretRef: "Choose the secret that signs requests.",
    });
    expect(
      triggerProblems({
        id: "t",
        kind: "webhook",
        name: "-bad",
        secretRef: "signing",
        maxBodyBytes: 0,
        toleranceSeconds: 301,
      }),
    ).toEqual({
      name: "Use letters, numbers, dots, underscores or hyphens, starting with a letter or number.",
      maxBodyBytes: "Use a size from 1 byte to 1 MiB (1,048,576 bytes).",
      toleranceSeconds: "Use a tolerance from 1 to 300 seconds.",
    });
    expect(
      triggerProblems({
        id: "t",
        kind: "webhook",
        name: "orders-in",
        secretRef: "signing",
      }),
    ).toEqual({});
  });

  it("keeps the test secret a valid handle of its own", () => {
    const hook = {
      id: "t",
      kind: "webhook" as const,
      name: "orders-in",
      secretRef: "signing",
    };
    expect(triggerProblems({ ...hook, testSecretRef: "signing-test" })).toEqual(
      {},
    );
    expect(triggerProblems({ ...hook, testSecretRef: "bad name" })).toEqual({
      testSecretRef:
        "Use letters, numbers, dots, underscores or hyphens, starting with a letter or number.",
    });
    expect(triggerProblems({ ...hook, testSecretRef: "signing" })).toEqual({
      testSecretRef: "Use a test secret other than the production secret.",
    });
  });

  it("uses the spec's words for a Kafka trigger", () => {
    expect(
      triggerProblems({
        id: "t",
        kind: "broker",
        name: "orders",
        topic: ".hidden",
      }),
    ).toEqual({
      clusterId: "Name the cluster, as configured on the Kafka connection.",
      topic:
        "Use a topic name with letters, numbers, dots, underscores or hyphens (at most 249).",
      deadLetterPolicy: "Choose what happens when a message can't start a run.",
    });
    expect(
      triggerProblems({
        id: "t",
        kind: "broker",
        name: "orders",
        clusterId: "main",
        topic: "x".repeat(250),
        deadLetterPolicy: "halt",
      }),
    ).toEqual({
      topic:
        "Use a topic name with letters, numbers, dots, underscores or hyphens (at most 249).",
    });
  });

  it("checks a schedule's calendar with the platform's rules", () => {
    expect(triggerProblems({ id: "t", kind: "schedule" })).toEqual({
      cron: "Choose when the schedule runs.",
    });
    expect(
      triggerProblems({ id: "t", kind: "schedule", cron: "0 9 1 * 1" }),
    ).toEqual({
      cron: "Set the day of month or the day of week, not both. Leave the other as *.",
    });
  });

  it("asks an app event trigger for its app", () => {
    expect(
      triggerProblems({ id: "t", kind: "provider", name: "support" }),
    ).toEqual({ provider: "Choose the app and event." });
  });
});

describe("step details lines", () => {
  it("previews a schedule's next five times in UTC, and says why a calendar can't run", () => {
    expect(
      schedulePreview("0 9 * * 1", new Date("2026-11-18T10:00:00Z")),
    ).toEqual({
      name: "Mondays 09:00 UTC",
      next: [
        "Mon 23 Nov, 09:00 UTC",
        "Mon 30 Nov, 09:00 UTC",
        "Mon 7 Dec, 09:00 UTC",
        "Mon 14 Dec, 09:00 UTC",
        "Mon 21 Dec, 09:00 UTC",
      ],
    });
    expect(
      schedulePreview("0 0 31 * *", new Date("2026-11-18T10:00:00Z")),
    ).toMatchObject({ note: "Months without day 31 are skipped." });
    expect(schedulePreview("0 24 * * *", new Date())).toEqual({
      problem: "The hour must be between 0 and 23.",
    });
  });

  it("names the mailbox of the email connection bound here, or when to choose it", () => {
    expect(mailboxLine({ name: "mail-dev", mailbox: "INBOX" })).toBe(
      "mail-dev · INBOX",
    );
    expect(mailboxLine(undefined)).toBe(
      "Choose the email connection when you activate.",
    );
  });

  it("says when a Kafka connection serves another cluster", () => {
    expect(
      kafkaClusterMismatch("orders-kafka", "orders-cluster", "local-kafka"),
    ).toBe("orders-kafka serves cluster orders-cluster, not local-kafka.");
    expect(kafkaClusterMismatch("orders-kafka", "main", "main")).toBeNull();
  });
});

describe("the input-fit question", () => {
  // The received message's schema, as the platform's email_input_schema() inlines it (abridged).
  const EMAIL = {
    type: "object",
    properties: {
      sender: { type: "string" },
      to: { type: "array", items: { type: "string" } },
      subject: { type: "string" },
      reply_to: { anyOf: [{ type: "string" }, { type: "null" }] },
      automated: { type: "boolean" },
    },
    required: ["sender", "to", "subject"],
  };
  const FORM = {
    type: "object",
    properties: { region: { type: "string" } },
    required: ["region"],
  };

  it.each([
    [
      "part of the message",
      {
        type: "object",
        properties: { sender: { type: "string" } },
        required: ["sender"],
      },
    ],
    [
      "fields the message may lack, optional",
      {
        type: "object",
        properties: { sender: { type: "string" }, note: { type: "string" } },
      },
    ],
    [
      "a wider type",
      {
        type: "object",
        properties: { to: { type: ["array", "null"] }, automated: {} },
      },
    ],
    [
      "a field whose message type it can't read",
      { type: "object", properties: { reply_to: { type: "string" } } },
    ],
  ])("lets an email start a workflow whose input asks for %s", (_, input) => {
    expect(inputFit("email", input, EMAIL)).toEqual({ kind: "fits" });
    expect(inputFit("email", input, EMAIL, { existing: true })).toEqual({
      kind: "fits",
    });
  });

  it.each([
    [
      "a field the message doesn't have",
      { type: "object", properties: {}, required: ["orderId"] },
    ],
    [
      "another type for a message field",
      { type: "object", properties: { automated: { type: "string" } } },
    ],
    [
      "only the fields it names",
      {
        type: "object",
        properties: { sender: { type: "string" } },
        additionalProperties: false,
      },
    ],
    ["something other than an object", { type: "array" }],
  ])("asks when the input asks for %s", (_, input) => {
    expect(inputFit("email", input, EMAIL).kind).toBe("ask");
  });

  it("asks before replacing an input that doesn't take an email", () => {
    expect(inputFit("email", FORM, EMAIL)).toEqual({
      kind: "ask",
      question:
        "The email message doesn't fit this workflow's input. Replace the input with the email message?",
      replace: "Replace",
      cancel: "Cancel",
      schema: EMAIL,
    });
  });

  it("fills an empty input without asking, and leaves a fitting one alone", () => {
    expect(inputFit("email", { type: "object" }, EMAIL)).toEqual({
      kind: "replace",
      schema: EMAIL,
    });
    expect(inputFit("email", EMAIL, EMAIL)).toEqual({ kind: "fits" });
  });

  it("never asks for triggers whose payload is the workflow's input", () => {
    for (const kind of [
      "webhook",
      "broker",
      "schedule",
      "manual",
      "called",
      "provider",
    ] as const)
      expect(inputFit(kind, FORM, EMAIL)).toEqual({ kind: "fits" });
  });

  it("says when an email trigger's workflow no longer takes an email", () => {
    expect(inputFit("email", FORM, EMAIL, { existing: true })).toEqual({
      kind: "problem",
      message:
        "This workflow's input no longer accepts an email. Reset the input to the email message.",
      schema: EMAIL,
    });
  });
});
