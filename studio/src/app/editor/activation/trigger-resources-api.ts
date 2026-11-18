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
// The platform resources behind a workflow's triggers, in one environment:
// webhooks, schedules, Kafka triggers, email sources and app event sources.
// Studio lists them to find the ones this workflow's activations run, creates
// them after an activation, and moves them to the next activation with the
// revision it read, so a webhook keeps its URL across versions.
import { ApiError, type StudioApi } from "../../api";
import type { Json, JsonObject } from "../../forms/core/json";
import type { TriggerIntent, TriggerKind } from "../state/canvas-sidecar";

/** The trigger kinds that have a platform resource. */
export type ResourceKind = Exclude<TriggerKind, "manual" | "called">;
export const RESOURCE_KINDS: readonly ResourceKind[] = [
  "webhook",
  "schedule",
  "broker",
  "email",
  "provider",
];
/** The kinds that move with `POST …/retarget`; a schedule moves by saving it. */
export type RetargetKind = Exclude<ResourceKind, "schedule">;

/** What this client needs from `StudioApi`. */
export type TriggerApi = Pick<
  StudioApi,
  "environment" | "page" | "request" | "mutate"
>;

/** `triggers.read`. */
export interface WebhookView {
  id: string;
  name: string;
  kind: "run" | "signal";
  activation_id: string | null;
  run_id?: string | null;
  signal?: string | null;
  secret_ref: string;
  payload_schema: JsonObject;
  max_body_bytes: number;
  tolerance_seconds: number;
  principal_id: string;
  disabled: boolean;
  revision: number;
}
/** `schedules.read`. */
export interface ScheduleView {
  id: string;
  cron: string;
  timezone: "UTC";
  missed_policy: "skip";
  activation_id: string;
  input: Json;
  revision: number;
  principal_id: string;
  status: "enabled" | "disabled" | "blocked" | "deleted";
  next_due_at: string;
  blocked_reason: string | null;
}
/** `broker_triggers.read`. */
export interface BrokerTriggerView {
  id: string;
  name: string;
  driver: "kafka";
  connection_revision_id: string;
  cluster_id: string;
  topic: string;
  kind: "run" | "signal";
  activation_id: string | null;
  payload_schema: JsonObject;
  dead_letter_policy: "receipt" | "halt";
  binding_id: string;
  principal_id: string;
  disabled: boolean;
  blocked: boolean;
  revision: number;
}
/** `provider_sources.read`. */
export interface ProviderSourceView {
  id: string;
  name: string;
  provider: string;
  package: string;
  package_version: string;
  adapter_version: string;
  schema_digest: string;
  connection_revision_id: string;
  policy: JsonObject;
  kind: "run" | "signal";
  activation_id: string | null;
  mapping: JsonObject;
  binding_id: string;
  principal_id: string;
  disabled: boolean;
  revision: number;
}
/** `email_sources.read`: what an email source reads and starts, never its mail. */
export interface EmailSourceView {
  id: string;
  connection_revision_id: string;
  activation_id: string | null;
  mailbox: string;
  revision: number;
  disabled: boolean;
}
export interface ResourceViews {
  webhook: WebhookView;
  schedule: ScheduleView;
  broker: BrokerTriggerView;
  email: EmailSourceView;
  provider: ProviderSourceView;
}

/** One platform resource, with what matching and moving it needs. */
export interface TriggerResource<K extends ResourceKind = ResourceKind> {
  kind: K;
  id: string;
  /** The name people know it by: the resource's name, a schedule's calendar, an email source's mailbox. */
  name: string;
  activationId: string | null;
  revision: number;
  /** True while it starts nothing: stopped, blocked, or no longer reading. */
  disabled: boolean;
  view: ResourceViews[K];
}
export interface ResourceList<K extends ResourceKind = ResourceKind> {
  items: TriggerResource<K>[];
  /** True when there were more pages than Studio reads. */
  truncated: boolean;
}

export type RefusalKind =
  | "changed"
  | "schema-changed"
  | "stopped"
  | "other-workflow"
  | "not-run"
  | "no-revision"
  | "forbidden"
  | "missing"
  | "incomplete"
  | "failed";
export interface ResourceRefusal {
  kind: RefusalKind;
  /** The HTTP status; 0 when Studio refused before sending anything. */
  status: number;
  code: string;
  message: string;
}
export type ResourceOutcome<K extends ResourceKind = ResourceKind> =
  | { ok: true; resource: TriggerResource<K> }
  | { ok: false; refusal: ResourceRefusal };

/** Where a new resource goes: the activation it starts, and the connection the dialog chose. */
export interface ResourceTarget {
  activationId: string;
  inputSchema: JsonObject;
  connectionRevisionId?: string;
}

/** Studio reads at most this many pages of each kind before it says the list is too long. */
export const MAX_PAGES = 10;

const COLLECTIONS: Record<ResourceKind, string> = {
  webhook: "triggers",
  schedule: "schedules",
  broker: "broker-triggers",
  email: "email/sources",
  provider: "provider-sources",
};

const path = (api: TriggerApi, kind: ResourceKind, id?: string) =>
  `${api.environment}/${COLLECTIONS[kind]}${id === undefined ? "" : `/${encodeURIComponent(id)}`}`;

function resource<K extends ResourceKind>(
  kind: K,
  view: ResourceViews[K],
): TriggerResource<K> {
  const name =
    "name" in view
      ? view.name
      : "mailbox" in view
        ? view.mailbox
        : (view as ScheduleView).cron;
  const disabled =
    "status" in view
      ? view.status !== "enabled"
      : view.disabled || ("blocked" in view && view.blocked);
  return {
    kind,
    id: view.id,
    name,
    activationId: view.activation_id ?? null,
    revision: view.revision,
    disabled,
    view,
  };
}

/** Every resource of one kind in the environment, at most `MAX_PAGES` pages. */
export async function listTriggerResources<K extends ResourceKind>(
  api: TriggerApi,
  kind: K,
): Promise<ResourceList<K>> {
  const items: TriggerResource<K>[] = [];
  let cursor: string | undefined;
  for (let page = 0; page < MAX_PAGES; page++) {
    const answer = await api.page(COLLECTIONS[kind], true, cursor);
    for (const item of answer.items)
      items.push(resource(kind, item as unknown as ResourceViews[K]));
    if (answer.next_cursor === null) return { items, truncated: false };
    cursor = answer.next_cursor;
  }
  return { items, truncated: true };
}

/** One resource as it is now, with the revision a move must send. */
export async function readResource<K extends ResourceKind>(
  api: TriggerApi,
  kind: K,
  id: string,
): Promise<TriggerResource<K>> {
  return resource(
    kind,
    await api.request<ResourceViews[K]>(path(api, kind, id)),
  );
}

const refusalKinds: Record<string, RefusalKind> = {
  "WV-REVISION": "changed",
  "WV-SCHEDULE-REVISION": "changed",
  "WV-TRIGGER-SCHEMA-CHANGED": "schema-changed",
  "WV-TRIGGER-DISABLED": "stopped",
  "WV-TRIGGER-WORKFLOW": "other-workflow",
  "WV-TRIGGER-KIND": "not-run",
  "WV-ETAG": "no-revision",
};

function refusal(error: ApiError): ResourceRefusal {
  const kind =
    refusalKinds[error.code] ??
    (error.status === 403
      ? "forbidden"
      : error.status === 404
        ? "missing"
        : "failed");
  return {
    kind,
    status: error.status,
    code: error.code,
    message: error.message,
  };
}

async function outcome<K extends ResourceKind>(
  kind: K,
  send: () => Promise<ResourceViews[K]>,
): Promise<ResourceOutcome<K>> {
  try {
    return { ok: true, resource: resource(kind, await send()) };
  } catch (error) {
    if (error instanceof ApiError)
      return { ok: false, refusal: refusal(error) };
    throw error;
  }
}

/** Move a trigger to another activation of the same workflow; `revision` is the one Studio read. */
export function retarget<K extends RetargetKind>(
  api: TriggerApi,
  kind: K,
  id: string,
  activationId: string,
  revision: number,
): Promise<ResourceOutcome<K>> {
  return outcome(kind, () =>
    api.mutate<ResourceViews[K]>(
      `${path(api, kind, id)}/retarget`,
      "POST",
      { activation_id: activationId },
      revision,
    ),
  );
}

/** Move a schedule to another activation by saving it, optionally with a new run input. */
export function moveSchedule(
  api: TriggerApi,
  schedule: ScheduleView,
  activationId: string,
  input: Json = schedule.input,
): Promise<ResourceOutcome<"schedule">> {
  return outcome("schedule", () =>
    api.mutate<ScheduleView>(
      path(api, "schedule"),
      "POST",
      {
        id: schedule.id,
        cron: schedule.cron,
        timezone: "UTC",
        missed_policy: "skip",
        activation_id: activationId,
        input,
      },
      schedule.revision,
    ),
  );
}

const text = (intent: TriggerIntent, key: string) => {
  const value = intent[key];
  return typeof value === "string" && value.trim() ? value : undefined;
};
const whole = (intent: TriggerIntent, key: string) => {
  const value = intent[key];
  return typeof value === "number" && Number.isInteger(value)
    ? value
    : undefined;
};
const object = (intent: TriggerIntent, key: string) => {
  const value = intent[key];
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as JsonObject)
    : undefined;
};

const incomplete = (message: string): ResourceOutcome<never> => ({
  ok: false,
  refusal: { kind: "incomplete", status: 0, code: "", message },
});

/** The request that creates the resource for a trigger choice, or what is still to be chosen. */
function creation(
  intent: TriggerIntent & { kind: ResourceKind },
  target: ResourceTarget,
): { body: Record<string, unknown> } | { missing: string } {
  const start = { kind: "run", activation_id: target.activationId };
  switch (intent.kind) {
    case "webhook": {
      const name = text(intent, "name");
      const secret = text(intent, "secretRef");
      if (!name) return { missing: "Name the webhook before activating." };
      if (!secret)
        return { missing: "Choose the webhook's secret before activating." };
      return {
        body: {
          name,
          ...start,
          secret_ref: secret,
          payload_schema: target.inputSchema,
          max_body_bytes: whole(intent, "maxBodyBytes") ?? 1048576,
          tolerance_seconds: whole(intent, "toleranceSeconds") ?? 300,
        },
      };
    }
    case "schedule": {
      const cron = text(intent, "cron");
      if (!cron)
        return { missing: "Choose when the schedule runs before activating." };
      return {
        body: {
          cron,
          timezone: "UTC",
          missed_policy: "skip",
          activation_id: target.activationId,
          input: (intent["input"] as Json | undefined) ?? null,
        },
      };
    }
    case "broker": {
      const name = text(intent, "name");
      const cluster = text(intent, "clusterId");
      const topic = text(intent, "topic");
      const policy = text(intent, "deadLetterPolicy");
      if (!name || !cluster || !topic || !policy)
        return {
          missing: "Finish the Kafka trigger's settings before activating.",
        };
      if (!target.connectionRevisionId)
        return { missing: "Choose a Kafka connection before activating." };
      return {
        body: {
          name,
          driver: "kafka",
          connection_revision_id: target.connectionRevisionId,
          cluster_id: cluster,
          topic,
          ...start,
          payload_schema: target.inputSchema,
          dead_letter_policy: policy,
        },
      };
    }
    case "email":
      if (!target.connectionRevisionId)
        return { missing: "Choose an email connection before activating." };
      return {
        body: {
          connection_revision_id: target.connectionRevisionId,
          activation_id: target.activationId,
        },
      };
    case "provider": {
      const fields = {
        name: text(intent, "name"),
        provider: text(intent, "provider"),
        package: text(intent, "package"),
        package_version: text(intent, "packageVersion"),
        schema_digest: text(intent, "schemaDigest"),
      };
      const policy = object(intent, "policy");
      if (Object.values(fields).some((value) => !value) || !policy)
        return {
          missing: "Finish the app event's settings before activating.",
        };
      if (!target.connectionRevisionId)
        return { missing: "Choose the app's connection before activating." };
      const mapping = object(intent, "mapping");
      const adapter = text(intent, "adapterVersion");
      return {
        body: {
          ...fields,
          ...(adapter ? { adapter_version: adapter } : {}),
          connection_revision_id: target.connectionRevisionId,
          policy,
          ...start,
          ...(mapping ? { mapping } : {}),
        },
      };
    }
  }
}

/** Create the resource a trigger choice describes, for the activation that just succeeded. */
export async function createResource<K extends ResourceKind>(
  api: TriggerApi,
  intent: TriggerIntent & { kind: K },
  target: ResourceTarget,
): Promise<ResourceOutcome<K>> {
  const request = creation(intent, target);
  if ("missing" in request) return incomplete(request.missing);
  const kind = intent.kind;
  if (kind !== "email")
    return outcome(kind, () =>
      api.mutate<ResourceViews[K]>(path(api, kind), "POST", request.body),
    );
  // Creating an email source answers its ID only; the read gives its mailbox and revision.
  return outcome(kind, async () => {
    const created = await api.mutate<{ id: string }>(
      path(api, kind),
      "POST",
      request.body,
    );
    return api.request<ResourceViews[K]>(path(api, kind, created.id));
  });
}
