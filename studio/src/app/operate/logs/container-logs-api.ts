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
// Container log requests and what choosing one needs: the deployment
// targets, their runners, and the worker components and members the newest
// observation saw. Every answer goes through a typed reader, so a snapshot's
// lines, the runner's state and the members offered come only from records
// whose every field has the contract's shape; an answer that isn't the record
// asked for is a failed read.
import { ApiError } from "../../api";

/** What the container logs read and send through: the current platform. */
export interface ContainerLogsApi {
  readonly environment: string;
  request<T>(
    path: string,
    method?: string,
    body?: unknown,
    headers?: Record<string, string>,
  ): Promise<T>;
}
export interface LogTarget {
  id: string;
  name: string;
  /** read_logs among them: the target's runner may read its logs. */
  capabilities: string[];
  disabled: boolean;
}
export interface LogRunner {
  targetId: string;
  capabilities: string[];
  lastSeen: string;
  /** In touch until then (90 s after its last contact). */
  expiresAt: string;
  revoked: boolean;
}
/** A worker component and the members (container ID prefixes or pod names) last observed. */
export interface LogComponent {
  name: string;
  members: string[];
}
export type LogStream = "stdout" | "stderr" | "output";
export interface LogLine {
  at: string;
  stream: LogStream;
  text: string;
}
export interface MemberLog {
  member: string;
  lines: LogLine[];
}
export type LogRequestState = "pending" | "ready" | "failed" | "expired";
export type LogFailureCode =
  | "no_answer"
  | "unknown_component"
  | "no_members"
  | "unreadable";
export interface LogRequestView {
  id: string;
  targetId: string;
  component: string;
  requestedMembers: string[];
  sinceSeconds: number;
  tailLines: number;
  state: LogRequestState;
  createdAt: string;
  expiresAt: string;
  readAt: string | null;
  members: MemberLog[];
  truncated: boolean;
  failure: LogFailureCode | null;
}
export interface LogRequestCreate {
  target_id: string;
  component: string;
  members: string[];
  since_seconds: number;
  tail_lines: number;
}

type Json = Record<string, unknown>;
const isRecord = (value: unknown): value is Json =>
  !!value && typeof value === "object" && !Array.isArray(value);
const isText = (value: unknown): value is string => typeof value === "string";
const isTexts = (value: unknown): value is string[] =>
  Array.isArray(value) && value.every(isText);
const isInstant = (value: unknown): value is string =>
  isText(value) && Number.isFinite(Date.parse(value));
const isCount = (value: unknown, low: number, high: number) =>
  Number.isSafeInteger(value) &&
  (value as number) >= low &&
  (value as number) <= high;
const streams: readonly unknown[] = ["stdout", "stderr", "output"];
const states: readonly unknown[] = ["pending", "ready", "failed", "expired"];
const failures: readonly unknown[] = [
  "no_answer",
  "unknown_component",
  "no_members",
  "unreadable",
];

/** What a failed read says when the answer isn't what was asked for. */
const unreadableAnswer = () =>
  new ApiError(502, {
    code: "WV-STUDIO-ANSWER",
    message: "Studio couldn't read the platform's answer.",
  });
const items = (value: unknown): unknown[] => {
  if (!isRecord(value) || !Array.isArray(value["items"]))
    throw unreadableAnswer();
  return value["items"];
};

/** The targets of a page; one without every field Studio reads is left out. */
export function readTargets(value: unknown): LogTarget[] {
  return items(value).flatMap((item) =>
    isRecord(item) &&
    isText(item["id"]) &&
    isText(item["name"]) &&
    isTexts(item["capabilities"]) &&
    typeof item["disabled"] === "boolean"
      ? [
          {
            id: item["id"],
            name: item["name"],
            capabilities: [...item["capabilities"]],
            disabled: item["disabled"],
          },
        ]
      : [],
  );
}
export function readRunners(value: unknown): LogRunner[] {
  return items(value).flatMap((item) =>
    isRecord(item) &&
    isText(item["target_id"]) &&
    isTexts(item["capabilities"]) &&
    isInstant(item["last_seen"]) &&
    isInstant(item["expires_at"]) &&
    typeof item["revoked"] === "boolean"
      ? [
          {
            targetId: item["target_id"],
            capabilities: [...item["capabilities"]],
            lastSeen: item["last_seen"],
            expiresAt: item["expires_at"],
            revoked: item["revoked"],
          },
        ]
      : [],
  );
}
/**
 * The worker components of a target's newest complete observation, with the
 * members it listed; [] when it has none Studio can read.
 */
export function readComponents(
  value: unknown,
  targetId: string,
): LogComponent[] {
  const newest = items(value)
    .filter(
      (item): item is Json =>
        isRecord(item) &&
        item["target_id"] === targetId &&
        item["complete"] === true &&
        isInstant(item["observed_at"]) &&
        Array.isArray(item["resources"]),
    )
    .sort((a, b) =>
      String(b["observed_at"]).localeCompare(String(a["observed_at"])),
    )[0];
  if (!newest) return [];
  return (newest["resources"] as unknown[]).flatMap((resource) => {
    if (
      !isRecord(resource) ||
      resource["kind"] !== "worker" ||
      !isText(resource["name"])
    )
      return [];
    const members = Array.isArray(resource["members"])
      ? resource["members"].flatMap((member) =>
          isRecord(member) && isText(member["host"]) ? [member["host"]] : [],
        )
      : [];
    return [{ name: resource["name"], members }];
  });
}

function readLine(value: unknown): LogLine | null {
  return isRecord(value) &&
    isInstant(value["at"]) &&
    streams.includes(value["stream"]) &&
    isText(value["text"])
    ? {
        at: value["at"],
        stream: value["stream"] as LogStream,
        text: value["text"],
      }
    : null;
}
function readMember(value: unknown): MemberLog | null {
  if (
    !isRecord(value) ||
    !isText(value["member"]) ||
    !Array.isArray(value["lines"])
  )
    return null;
  const lines = value["lines"].map(readLine);
  return lines.every((line): line is LogLine => line !== null)
    ? { member: value["member"], lines }
    : null;
}
/** A log request as deployment_log_requests answers it; throws for anything else. */
export function readLogRequest(value: unknown): LogRequestView {
  if (
    !isRecord(value) ||
    !isText(value["id"]) ||
    !isText(value["target_id"]) ||
    !isText(value["component"]) ||
    !isTexts(value["requested_members"]) ||
    !isCount(value["since_seconds"], 60, 86400) ||
    !isCount(value["tail_lines"], 1, 1000) ||
    !states.includes(value["state"]) ||
    !isInstant(value["created_at"]) ||
    !isInstant(value["expires_at"]) ||
    !(
      value["read_at"] === null ||
      value["read_at"] === undefined ||
      isInstant(value["read_at"])
    ) ||
    !Array.isArray(value["members"]) ||
    typeof value["truncated"] !== "boolean" ||
    !(
      value["failure"] === null ||
      value["failure"] === undefined ||
      failures.includes(value["failure"])
    )
  )
    throw unreadableAnswer();
  const members = value["members"].map(readMember);
  if (!members.every((member): member is MemberLog => member !== null))
    throw unreadableAnswer();
  return {
    id: value["id"],
    targetId: value["target_id"],
    component: value["component"],
    requestedMembers: [...value["requested_members"]],
    sinceSeconds: value["since_seconds"] as number,
    tailLines: value["tail_lines"] as number,
    state: value["state"] as LogRequestState,
    createdAt: value["created_at"],
    expiresAt: value["expires_at"],
    readAt: (value["read_at"] as string | null | undefined) ?? null,
    members,
    truncated: value["truncated"],
    failure: (value["failure"] as LogFailureCode | null | undefined) ?? null,
  };
}

const query = (values: Record<string, string>) =>
  "?" +
  Object.entries(values)
    .map(([key, value]) => `${key}=${encodeURIComponent(value)}`)
    .join("&");

export async function listTargets(api: ContainerLogsApi): Promise<LogTarget[]> {
  return readTargets(
    await api.request<unknown>(
      `${api.environment}/deployment-targets${query({ limit: "100" })}`,
    ),
  );
}
export async function listRunners(
  api: ContainerLogsApi,
  targetId: string,
): Promise<LogRunner[]> {
  return readRunners(
    await api.request<unknown>(
      `${api.environment}/deployment-runners${query({ target_id: targetId, limit: "100" })}`,
    ),
  );
}
export async function listComponents(
  api: ContainerLogsApi,
  targetId: string,
): Promise<LogComponent[]> {
  return readComponents(
    await api.request<unknown>(
      `${api.environment}/deployment-observations${query({ target_id: targetId, limit: "100" })}`,
    ),
    targetId,
  );
}
/** Asks the target's runner for a snapshot; `key` makes a resend the same request. */
export async function createLogRequest(
  api: ContainerLogsApi,
  body: LogRequestCreate,
  key: string,
): Promise<LogRequestView> {
  return readLogRequest(
    await api.request<unknown>(
      `${api.environment}/deployment-log-requests`,
      "POST",
      body,
      { "Idempotency-Key": key },
    ),
  );
}
export async function readLogRequestById(
  api: ContainerLogsApi,
  id: string,
): Promise<LogRequestView> {
  return readLogRequest(
    await api.request<unknown>(
      `${api.environment}/deployment-log-requests/${encodeURIComponent(id)}`,
    ),
  );
}
