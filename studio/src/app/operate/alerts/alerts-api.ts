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
// The platform's alert operations: rules and their preview, presets, email
// channels, alerts and their acknowledgement, the alert settings, outside
// recipients' opt-ins and each person's own alert address. Every record goes
// through its typed reader (readRule, readAlert, readChannel…), so what a
// screen shows comes only from a record whose every field has the contract's
// shape; a record that doesn't is dropped, and an answer that isn't a page at
// all is a failed read.
import { ApiError } from "../../api";

/** The signals this version of Studio has words for, in the platform's order. */
export const alertSignals = [
  "run.failure_rate",
  "run.failures",
  "run.duration_p95",
  "queue.oldest_ready_seconds",
  "queue.ready_tasks",
  "outbox.pending",
  "provider.pending",
  "incidents.active",
  "workers.online",
  "workers.free_slots",
  "targets.runner_offline",
  "deployments.reconciliation_required",
  "health.degraded",
] as const;
export type AlertSignal = (typeof alertSignals)[number];
export const alertUnits = ["count", "ratio", "seconds"] as const;
export type AlertUnit = (typeof alertUnits)[number];
export const comparators = ["gt", "gte", "lt", "lte"] as const;
export type Comparator = (typeof comparators)[number];
export const severities = ["info", "warning", "critical"] as const;
export type Severity = (typeof severities)[number];
/** The windows a window signal reads over, in seconds. */
export const alertWindows = [60, 300, 900, 3600] as const;
export const ruleStates = [
  "ok",
  "pending",
  "firing",
  "unsupported",
  "unauthorized",
] as const;
export type RuleStateName = (typeof ruleStates)[number];
export const resolvedReasons = [
  "threshold",
  "no_data",
  "rule_changed",
  "rule_disabled",
  "rule_deleted",
] as const;
export type ResolvedReason = (typeof resolvedReasons)[number];
const notificationKinds = ["firing", "resolved", "repeat", "test"] as const;
const notificationStates = [
  "pending",
  "sent",
  "failed",
  "skipped_limit",
] as const;
export const recipientStates = [
  "ok",
  "not_member",
  "without_address",
  "domain_not_allowed",
  "without_consent",
] as const;
export type RecipientState = (typeof recipientStates)[number];

export interface AlertSelector {
  workflow?: string;
  task_type?: string;
  target_id?: string;
}
export interface AlertRuleDraft {
  name: string;
  enabled: boolean;
  /** A signal this Studio knows, or a newer server's. */
  signal: string;
  selector: AlertSelector;
  comparator: Comparator;
  threshold: number;
  window_seconds: number | null;
  for_seconds: number;
  min_samples: number;
  severity: Severity;
  channel_ids: string[];
  renotify_seconds: number;
}
export interface RuleState {
  state: RuleStateName;
  since: string;
  value: number | null;
  unit: AlertUnit | null;
  evaluated_at: string | null;
  error: { code: string; at: string } | null;
  alert_id: string | null;
  no_data: boolean;
}
export interface AlertRule extends AlertRuleDraft {
  id: string;
  revision: number;
  preset_id: string | null;
  created_at: string;
  updated_at: string;
  updated_by: string | null;
  state: RuleState;
}
export interface AlertNotification {
  channel_id: string;
  kind: (typeof notificationKinds)[number];
  state: (typeof notificationStates)[number];
  code: string | null;
  at: string;
}
export interface Alert {
  id: string;
  rule_id: string;
  rule_name: string;
  rule_revision: number;
  signal: string;
  severity: Severity;
  state: "firing" | "resolved";
  started_at: string;
  resolved_at: string | null;
  resolved_reason: ResolvedReason | null;
  comparator: Comparator;
  threshold: number;
  unit: AlertUnit;
  value_at_start: number;
  last_value: number | null;
  acknowledged_at: string | null;
  acknowledged_by: string | null;
  notifications: AlertNotification[];
}
export interface AlertPage {
  items: Alert[];
  nextCursor: string | null;
  /** Firing alerts in the query's scope, and how many of them are critical. */
  firing: number;
  critical: number;
}
export type Recipient = { member: string } | { address: string };
export interface AlertChannelDraft {
  kind: "email";
  name: string;
  connection_revision_id: string;
  recipients: Recipient[];
}
export interface AlertChannel extends AlertChannelDraft {
  id: string;
  owner_id: string;
  revision: number;
  recipient_states: { index: number; state: RecipientState }[];
  status: "ok" | "owner_unauthorized";
}
export interface AlertPreset {
  id: string;
  version: number;
  install: "default" | "suggested";
  name: string;
  rule: AlertRuleDraft;
  installed_rule_id: string | null;
}
export interface AlertPreview {
  value: number | null;
  unit: AlertUnit;
  condition_met: boolean;
  samples: number | null;
  evaluated_at: string;
  no_data_reason: "no_data" | "too_few_samples" | null;
}
export interface AlertSettings {
  recipient_domains: string[];
  approval_channel_ids: string[];
  /** 0 until the settings are first saved. */
  revision: number;
}
export interface AlertSettingsUpdate {
  recipient_domains: string[];
  approval_channel_ids: string[];
}
export interface RecipientConsent {
  id: string;
  domain: string;
  address: string;
  method: "administrator_attested" | "recipient_confirmed";
  statement: string;
  created_by: string;
  created_at: string;
  revoked_at: string | null;
  revoked_by: string | null;
}
export interface MyAlertAddress {
  address: string | null;
  updated_at: string | null;
}

/** What the calls read through: Studio's API client, or a test's. */
export interface AlertsTransport {
  /** The environment's API path; throws without one. */
  readonly environment: string;
  /** The project's API path; throws without one. */
  readonly project: string;
  request<T>(
    path: string,
    method?: string,
    body?: unknown,
    headers?: Record<string, string>,
  ): Promise<T>;
}

type Json = Record<string, unknown>;
const isRecord = (value: unknown): value is Json =>
  !!value && typeof value === "object" && !Array.isArray(value);
const isText = (value: unknown): value is string =>
  typeof value === "string" && value.length > 0;
const isInstant = (value: unknown): value is string =>
  isText(value) && Number.isFinite(Date.parse(value));
const isNumber = (value: unknown): value is number =>
  typeof value === "number" && Number.isFinite(value);
const isCount = (value: unknown): value is number =>
  Number.isSafeInteger(value) && (value as number) >= 0;
const oneOf = <T extends string | number>(
  list: readonly T[],
  value: unknown,
): value is T => (list as readonly unknown[]).includes(value);
/** An optional field: absent (the platform leaves None out), or its shape. */
const optional = <T>(
  value: unknown,
  check: (value: unknown) => value is T,
): T | null | undefined =>
  value === undefined || value === null
    ? null
    : check(value)
      ? value
      : undefined;
const texts = (value: unknown): value is string[] =>
  Array.isArray(value) && value.every(isText);
const cursorPattern = /^[A-Za-z0-9_-]+$/;
const cursorOf = (value: unknown) =>
  isText(value) && cursorPattern.test(value) ? value : null;

/** What a failed read says when the answer isn't what the operation returns. */
export const unreadableAnswer = () =>
  new ApiError(502, {
    code: "WV-STUDIO-ANSWER",
    message: "Studio couldn't read the platform's answer.",
  });

function readSelector(value: unknown): AlertSelector | null {
  if (!isRecord(value)) return null;
  const selector: AlertSelector = {};
  for (const [key, field] of Object.entries(value)) {
    if (key !== "workflow" && key !== "task_type" && key !== "target_id")
      return null;
    if (field === null || field === undefined) continue;
    if (!isText(field)) return null;
    selector[key] = field;
  }
  return Object.keys(selector).length > 1 ? null : selector;
}

/** The draft fields of a rule or a preset's rule; null when one is off-shape. */
function readDraft(item: Json): AlertRuleDraft | null {
  const selector = readSelector(item["selector"] ?? {});
  const window = item["window_seconds"];
  if (
    !isText(item["name"]) ||
    typeof item["enabled"] !== "boolean" ||
    !isText(item["signal"]) ||
    !selector ||
    !oneOf(comparators, item["comparator"]) ||
    !isNumber(item["threshold"]) ||
    !(window === null || window === undefined || oneOf(alertWindows, window)) ||
    !isCount(item["for_seconds"]) ||
    !isCount(item["min_samples"]) ||
    !oneOf(severities, item["severity"]) ||
    !texts(item["channel_ids"]) ||
    !isCount(item["renotify_seconds"])
  )
    return null;
  return {
    name: item["name"],
    enabled: item["enabled"],
    signal: item["signal"],
    selector,
    comparator: item["comparator"],
    threshold: item["threshold"],
    window_seconds: (window as number | undefined) ?? null,
    for_seconds: item["for_seconds"],
    min_samples: item["min_samples"],
    severity: item["severity"],
    channel_ids: [...item["channel_ids"]],
    renotify_seconds: item["renotify_seconds"],
  };
}

function readState(value: unknown): RuleState | null {
  if (!isRecord(value)) return null;
  const unit = optional(value["unit"], (v): v is AlertUnit =>
    oneOf(alertUnits, v),
  );
  const reading = optional(value["value"], isNumber);
  const evaluated = optional(value["evaluated_at"], isInstant);
  const alert = optional(value["alert_id"], isText);
  const error = value["error"];
  const readError =
    error === undefined || error === null
      ? null
      : isRecord(error) && isText(error["code"]) && isInstant(error["at"])
        ? { code: error["code"], at: error["at"] }
        : undefined;
  if (
    !oneOf(ruleStates, value["state"]) ||
    !isInstant(value["since"]) ||
    typeof value["no_data"] !== "boolean" ||
    unit === undefined ||
    reading === undefined ||
    evaluated === undefined ||
    alert === undefined ||
    readError === undefined
  )
    return null;
  return {
    state: value["state"],
    since: value["since"],
    value: reading,
    unit,
    evaluated_at: evaluated,
    error: readError,
    alert_id: alert,
    no_data: value["no_data"],
  };
}

/** A rule, when every field has the shape alert_rules.read gives it. */
export function readRule(value: unknown): AlertRule | null {
  if (!isRecord(value)) return null;
  const draft = readDraft(value);
  const state = readState(value["state"]);
  const preset = optional(value["preset_id"], isText);
  const updatedBy = optional(value["updated_by"], isText);
  if (
    !draft ||
    !state ||
    !isText(value["id"]) ||
    !isCount(value["revision"]) ||
    !isInstant(value["created_at"]) ||
    !isInstant(value["updated_at"]) ||
    preset === undefined ||
    updatedBy === undefined
  )
    return null;
  return {
    ...draft,
    id: value["id"],
    revision: value["revision"],
    preset_id: preset,
    created_at: value["created_at"],
    updated_at: value["updated_at"],
    updated_by: updatedBy,
    state,
  };
}

function readNotification(value: unknown): AlertNotification | null {
  if (!isRecord(value)) return null;
  const code = optional(value["code"], isText);
  if (
    !isText(value["channel_id"]) ||
    !oneOf(notificationKinds, value["kind"]) ||
    !oneOf(notificationStates, value["state"]) ||
    code === undefined ||
    !isInstant(value["at"])
  )
    return null;
  return {
    channel_id: value["channel_id"],
    kind: value["kind"],
    state: value["state"],
    code,
    at: value["at"],
  };
}

/** An alert, when every field has the shape alerts.read gives it. */
export function readAlert(value: unknown): Alert | null {
  if (!isRecord(value)) return null;
  const resolvedAt = optional(value["resolved_at"], isInstant);
  const reason = optional(value["resolved_reason"], (v): v is ResolvedReason =>
    oneOf(resolvedReasons, v),
  );
  const last = optional(value["last_value"], isNumber);
  const acknowledgedAt = optional(value["acknowledged_at"], isInstant);
  const acknowledgedBy = optional(value["acknowledged_by"], isText);
  const notes = value["notifications"] ?? [];
  const notifications = Array.isArray(notes) ? notes.map(readNotification) : [];
  if (
    !isText(value["id"]) ||
    !isText(value["rule_id"]) ||
    !isText(value["rule_name"]) ||
    !isCount(value["rule_revision"]) ||
    !isText(value["signal"]) ||
    !oneOf(severities, value["severity"]) ||
    !(value["state"] === "firing" || value["state"] === "resolved") ||
    !isInstant(value["started_at"]) ||
    !oneOf(comparators, value["comparator"]) ||
    !isNumber(value["threshold"]) ||
    !oneOf(alertUnits, value["unit"]) ||
    !isNumber(value["value_at_start"]) ||
    resolvedAt === undefined ||
    reason === undefined ||
    last === undefined ||
    acknowledgedAt === undefined ||
    acknowledgedBy === undefined ||
    !Array.isArray(notes) ||
    notifications.some((note) => !note)
  )
    return null;
  return {
    id: value["id"],
    rule_id: value["rule_id"],
    rule_name: value["rule_name"],
    rule_revision: value["rule_revision"],
    signal: value["signal"],
    severity: value["severity"],
    state: value["state"],
    started_at: value["started_at"],
    resolved_at: resolvedAt,
    resolved_reason: reason,
    comparator: value["comparator"],
    threshold: value["threshold"],
    unit: value["unit"],
    value_at_start: value["value_at_start"],
    last_value: last,
    acknowledged_at: acknowledgedAt,
    acknowledged_by: acknowledgedBy,
    notifications: notifications as AlertNotification[],
  };
}

function readRecipient(value: unknown): Recipient | null {
  if (!isRecord(value)) return null;
  const keys = Object.keys(value);
  if (keys.length !== 1) return null;
  if (isText(value["member"])) return { member: value["member"] };
  if (isText(value["address"])) return { address: value["address"] };
  return null;
}

/** A channel, when every field has the shape alert_channels.read gives it. */
export function readChannel(value: unknown): AlertChannel | null {
  if (!isRecord(value)) return null;
  const list = value["recipients"];
  const recipients = Array.isArray(list) ? list.map(readRecipient) : [];
  const states = value["recipient_states"];
  const readStates = Array.isArray(states)
    ? states.map((item) =>
        isRecord(item) &&
        isCount(item["index"]) &&
        oneOf(recipientStates, item["state"])
          ? { index: item["index"], state: item["state"] }
          : null,
      )
    : [];
  if (
    value["kind"] !== "email" ||
    !isText(value["id"]) ||
    !isText(value["name"]) ||
    !isText(value["connection_revision_id"]) ||
    !isText(value["owner_id"]) ||
    !isCount(value["revision"]) ||
    !Array.isArray(list) ||
    recipients.some((recipient) => !recipient) ||
    !Array.isArray(states) ||
    readStates.some((state) => !state) ||
    !(value["status"] === "ok" || value["status"] === "owner_unauthorized")
  )
    return null;
  return {
    kind: "email",
    id: value["id"],
    name: value["name"],
    connection_revision_id: value["connection_revision_id"],
    owner_id: value["owner_id"],
    revision: value["revision"],
    recipients: recipients as Recipient[],
    recipient_states: readStates as AlertChannel["recipient_states"],
    status: value["status"],
  };
}

/** A preset, when it and its rule have their shapes. */
export function readPreset(value: unknown): AlertPreset | null {
  if (!isRecord(value) || !isRecord(value["rule"])) return null;
  const rule = readDraft(value["rule"]);
  const installed = optional(value["installed_rule_id"], isText);
  if (
    !rule ||
    !isText(value["id"]) ||
    !isCount(value["version"]) ||
    !(value["install"] === "default" || value["install"] === "suggested") ||
    !isText(value["name"]) ||
    installed === undefined
  )
    return null;
  return {
    id: value["id"],
    version: value["version"],
    install: value["install"],
    name: value["name"],
    rule,
    installed_rule_id: installed,
  };
}

/** A preview; throws for anything else, since the editor shows it whole. */
export function readPreview(value: unknown): AlertPreview {
  if (!isRecord(value)) throw unreadableAnswer();
  const reading = value["value"];
  const samples = optional(value["samples"], isCount);
  const reason = optional(
    value["no_data_reason"],
    (v): v is "no_data" => v === "no_data" || v === "too_few_samples",
  );
  if (
    !(reading === null || isNumber(reading)) ||
    !oneOf(alertUnits, value["unit"]) ||
    typeof value["condition_met"] !== "boolean" ||
    samples === undefined ||
    !isInstant(value["evaluated_at"]) ||
    reason === undefined
  )
    throw unreadableAnswer();
  return {
    value: reading,
    unit: value["unit"],
    condition_met: value["condition_met"],
    samples,
    evaluated_at: value["evaluated_at"],
    no_data_reason: reason,
  };
}

/** The alert settings; throws for anything else. */
export function readSettings(value: unknown): AlertSettings {
  if (
    !isRecord(value) ||
    !texts(value["recipient_domains"]) ||
    !texts(value["approval_channel_ids"]) ||
    !isCount(value["revision"])
  )
    throw unreadableAnswer();
  return {
    recipient_domains: [...value["recipient_domains"]],
    approval_channel_ids: [...value["approval_channel_ids"]],
    revision: value["revision"],
  };
}

/** An opt-in, when every field has its shape. */
export function readConsent(value: unknown): RecipientConsent | null {
  if (!isRecord(value)) return null;
  const revokedAt = optional(value["revoked_at"], isInstant);
  const revokedBy = optional(value["revoked_by"], isText);
  if (
    !isText(value["id"]) ||
    !isText(value["domain"]) ||
    !isText(value["address"]) ||
    !(
      value["method"] === "administrator_attested" ||
      value["method"] === "recipient_confirmed"
    ) ||
    !isText(value["statement"]) ||
    !isText(value["created_by"]) ||
    !isInstant(value["created_at"]) ||
    revokedAt === undefined ||
    revokedBy === undefined
  )
    return null;
  return {
    id: value["id"],
    domain: value["domain"],
    address: value["address"],
    method: value["method"],
    statement: value["statement"],
    created_by: value["created_by"],
    created_at: value["created_at"],
    revoked_at: revokedAt,
    revoked_by: revokedBy,
  };
}

/** The caller's own alert address; throws for anything else. */
export function readMyAddress(value: unknown): MyAlertAddress {
  if (!isRecord(value)) throw unreadableAnswer();
  const address = optional(value["address"], isText);
  const updated = optional(value["updated_at"], isInstant);
  if (address === undefined || updated === undefined) throw unreadableAnswer();
  return { address, updated_at: updated };
}

/** A page of records; the ones that don't read are dropped. */
function readPage<T>(
  value: unknown,
  reader: (item: unknown) => T | null,
): { items: T[]; nextCursor: string | null } {
  if (!isRecord(value) || !Array.isArray(value["items"]))
    throw unreadableAnswer();
  return {
    items: value["items"]
      .map(reader)
      .filter((item): item is T => item !== null),
    nextCursor: cursorOf(value["next_cursor"]),
  };
}

/** An alerts.list answer, with its firing and critical counts. */
export function readAlertPage(value: unknown): AlertPage {
  const page = readPage(value, readAlert);
  const record = value as Json;
  if (!isCount(record["firing"]) || !isCount(record["critical"]))
    throw unreadableAnswer();
  return { ...page, firing: record["firing"], critical: record["critical"] };
}

// ------------------------------------------------------------------ calls

const query = (values: Record<string, string | number | undefined>) => {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values))
    if (value !== undefined && value !== "") params.set(key, String(value));
  const text = params.toString();
  return text ? `?${text}` : "";
};
const id = (value: string) => encodeURIComponent(value);
const keyed = (key: string) => ({ "Idempotency-Key": key });
const revised = (revision: number) => ({ "If-Match": `"${revision}"` });
/** "/…/tenants/T" from the project's path. */
const tenantOf = (api: AlertsTransport) =>
  api.project.slice(0, api.project.lastIndexOf("/projects/"));

/**
 * Every page of a list, 100 at a time, while the platform gives a cursor:
 * opt-ins, members and subscriptions have no fixed limit. A cursor that comes
 * back a second time ends the list, so a faulty answer can't loop.
 */
async function listAll<T>(
  api: AlertsTransport,
  path: string,
  reader: (item: unknown) => T | null,
): Promise<T[]> {
  const items: T[] = [];
  const seen = new Set<string>();
  let cursor: string | null = null;
  do {
    if (cursor) seen.add(cursor);
    const answer: { items: T[]; nextCursor: string | null } = readPage(
      await api.request<unknown>(
        `${path}${query({ limit: 100, cursor: cursor ?? undefined })}`,
      ),
      reader,
    );
    items.push(...answer.items);
    cursor = answer.nextCursor;
  } while (cursor && !seen.has(cursor));
  return items;
}

const rulesPath = (api: AlertsTransport) => `${api.environment}/alert-rules`;
const channelsPath = (api: AlertsTransport) =>
  `${api.environment}/alert-channels`;
const consentsPath = (api: AlertsTransport) =>
  `${api.environment}/alert-recipient-consents`;
function readOrThrow<T>(reader: (value: unknown) => T | null) {
  return (value: unknown): T => {
    const read = reader(value);
    if (read === null) throw unreadableAnswer();
    return read;
  };
}
const ruleAnswer = readOrThrow(readRule);
const channelAnswer = readOrThrow(readChannel);
const alertAnswer = readOrThrow(readAlert);
const consentAnswer = readOrThrow(readConsent);

export const listRules = (api: AlertsTransport) =>
  listAll(api, rulesPath(api), readRule);
export async function createRule(
  api: AlertsTransport,
  draft: AlertRuleDraft,
  key: string,
): Promise<AlertRule> {
  return ruleAnswer(
    await api.request(rulesPath(api), "POST", draft, keyed(key)),
  );
}
export async function updateRule(
  api: AlertsTransport,
  ruleId: string,
  revision: number,
  draft: AlertRuleDraft,
): Promise<AlertRule> {
  return ruleAnswer(
    await api.request(
      `${rulesPath(api)}/${id(ruleId)}`,
      "PUT",
      draft,
      revised(revision),
    ),
  );
}
export async function deleteRule(
  api: AlertsTransport,
  ruleId: string,
  revision: number,
): Promise<void> {
  await api.request(
    `${rulesPath(api)}/${id(ruleId)}`,
    "DELETE",
    undefined,
    revised(revision),
  );
}
export async function previewRule(
  api: AlertsTransport,
  draft: AlertRuleDraft,
): Promise<AlertPreview> {
  return readPreview(
    await api.request(`${rulesPath(api)}/preview`, "POST", draft),
  );
}
export async function listPresets(
  api: AlertsTransport,
): Promise<AlertPreset[]> {
  const answer = await api.request<unknown>(`${api.environment}/alert-presets`);
  if (!Array.isArray(answer)) throw unreadableAnswer();
  return answer
    .map(readPreset)
    .filter((preset): preset is AlertPreset => preset !== null);
}

export const listChannels = (api: AlertsTransport) =>
  listAll(api, channelsPath(api), readChannel);
export async function createChannel(
  api: AlertsTransport,
  draft: AlertChannelDraft,
  key: string,
): Promise<AlertChannel> {
  return channelAnswer(
    await api.request(channelsPath(api), "POST", draft, keyed(key)),
  );
}
export async function updateChannel(
  api: AlertsTransport,
  channelId: string,
  revision: number,
  draft: AlertChannelDraft,
): Promise<AlertChannel> {
  return channelAnswer(
    await api.request(
      `${channelsPath(api)}/${id(channelId)}`,
      "PUT",
      draft,
      revised(revision),
    ),
  );
}
export async function deleteChannel(
  api: AlertsTransport,
  channelId: string,
  revision: number,
): Promise<void> {
  await api.request(
    `${channelsPath(api)}/${id(channelId)}`,
    "DELETE",
    undefined,
    revised(revision),
  );
}
export async function testChannel(
  api: AlertsTransport,
  channelId: string,
  key: string,
): Promise<AlertNotification> {
  const answer = readNotification(
    await api.request(
      `${channelsPath(api)}/${id(channelId)}/test`,
      "POST",
      undefined,
      keyed(key),
    ),
  );
  if (!answer) throw unreadableAnswer();
  return answer;
}

export interface AlertQuery {
  state?: "firing" | "resolved";
  from?: string;
  limit?: number;
  cursor?: string;
}
export async function listAlerts(
  api: AlertsTransport,
  filters: AlertQuery,
): Promise<AlertPage> {
  return readAlertPage(
    await api.request(
      `${api.environment}/alerts${query({
        state: filters.state,
        from: filters.from,
        limit: filters.limit,
        cursor: filters.cursor,
      })}`,
    ),
  );
}
export async function acknowledgeAlert(
  api: AlertsTransport,
  alertId: string,
): Promise<Alert> {
  return alertAnswer(
    await api.request(
      `${api.environment}/alerts/${id(alertId)}/acknowledge`,
      "POST",
    ),
  );
}

export async function readAlertSettings(
  api: AlertsTransport,
): Promise<AlertSettings> {
  return readSettings(await api.request(`${api.environment}/alert-settings`));
}
/** Saves the settings; revision 0 means they were never saved, so no If-Match. */
export async function saveAlertSettings(
  api: AlertsTransport,
  update: AlertSettingsUpdate,
  revision: number,
): Promise<AlertSettings> {
  return readSettings(
    await api.request(
      `${api.environment}/alert-settings`,
      "PUT",
      update,
      revision > 0 ? revised(revision) : {},
    ),
  );
}

export const listConsents = (api: AlertsTransport) =>
  listAll(api, consentsPath(api), readConsent);
export async function recordConsent(
  api: AlertsTransport,
  draft: { address: string; statement: string },
  key: string,
): Promise<RecipientConsent> {
  return consentAnswer(
    await api.request(consentsPath(api), "POST", draft, keyed(key)),
  );
}
export async function revokeConsent(
  api: AlertsTransport,
  consentId: string,
): Promise<RecipientConsent> {
  return consentAnswer(
    await api.request(`${consentsPath(api)}/${id(consentId)}/revoke`, "POST"),
  );
}

export async function readMyAlertAddress(
  api: AlertsTransport,
): Promise<MyAlertAddress> {
  return readMyAddress(await api.request(`${tenantOf(api)}/my-alert-address`));
}
/** Saves the caller's own address; null removes it. */
export async function saveMyAlertAddress(
  api: AlertsTransport,
  address: string | null,
): Promise<MyAlertAddress> {
  return readMyAddress(
    await api.request(`${tenantOf(api)}/my-alert-address`, "PUT", { address }),
  );
}

/** How many active webhook subscriptions receive alert changes. */
export async function countAlertWebhooks(
  api: AlertsTransport,
): Promise<number> {
  const answers = await listAll(
    api,
    `${api.environment}/subscriptions`,
    (item) =>
      isRecord(item) &&
      item["active"] !== false &&
      Array.isArray(item["event_types"]) &&
      item["event_types"].includes("alert.transition")
        ? item
        : null,
  );
  return answers.length;
}

/** A person with a role in this project: a member a channel can name. */
export interface ProjectMember {
  id: string;
}
/** The project's people, from members.list (grant.manage or grant.admin). */
export async function listProjectMembers(
  api: AlertsTransport,
  projectId: string,
): Promise<ProjectMember[]> {
  const bindings = await listAll(api, `${tenantOf(api)}/members`, (item) =>
    isRecord(item) &&
    isText(item["principal_id"]) &&
    item["kind"] !== "worker" &&
    item["active"] !== false &&
    isRecord(item["scope"]) &&
    (item["scope"]["project_id"] === null ||
      item["scope"]["project_id"] === undefined ||
      item["scope"]["project_id"] === projectId)
      ? item["principal_id"]
      : null,
  );
  return [...new Set(bindings)].map((principal) => ({ id: principal }));
}

/** Deployment targets by ID, for rules that name one. */
export async function listTargetNames(
  api: AlertsTransport,
): Promise<Map<string, string>> {
  const targets = await listAll(
    api,
    `${api.environment}/deployment-targets`,
    (item) =>
      isRecord(item) && isText(item["id"]) && isText(item["name"])
        ? ([item["id"], item["name"]] as const)
        : null,
  );
  return new Map(targets);
}
