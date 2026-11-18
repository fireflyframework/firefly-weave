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
// What step details ask the platform about connections in the current
// environment: the ones this person may bind, a connection test and the
// environment's test policy. Answers are read field by field; the platform
// decides what may be bound and tested, Studio only shows it.
import { ApiError, type StudioApi } from "../../api";
import type { EnvironmentTestPolicy } from "../state/execution";

export type TestReason =
  | "unreachable"
  | "destination"
  | "unauthorized"
  | "status";

/** A connection this person may bind in the environment, as the platform lists it. */
export interface BindableItem {
  connectionId: string;
  revisionId: string;
  name: string;
  connector: string;
  enabled: boolean;
  /** Scheme, host and port of an HTTP connection's address; null for other connectors. */
  origin: string | null;
}

/** A connection test's outcome; `message` is the platform's own sentence when it couldn't run the test. */
export interface ConnectionTest {
  ok: boolean;
  reason: TestReason | null;
  encrypted: boolean | null;
  message: string | null;
}

export interface ConnectionsApi {
  bindable(connector: string): Promise<BindableItem[]>;
  test(revisionId: string): Promise<ConnectionTest>;
  policy(): Promise<EnvironmentTestPolicy | "unreadable">;
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const REASONS: ReadonlySet<string> = new Set([
  "unreachable",
  "destination",
  "unauthorized",
  "status",
]);
const PAGE = 100;
const MAX_PAGES = 20;

const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const isUuid = (value: unknown): value is string =>
  typeof value === "string" && UUID.test(value);

/** A page's items with every field typed; malformed items are dropped. */
export function readBindable(value: unknown): BindableItem[] {
  if (!isRecord(value) || !Array.isArray(value["items"])) return [];
  const items: BindableItem[] = [];
  for (const raw of value["items"]) {
    if (!isRecord(raw)) continue;
    const {
      connection_id: connectionId,
      revision_id: revisionId,
      name,
      connector,
      enabled,
      endpoint_origin: origin,
    } = raw;
    if (!isUuid(connectionId) || !isUuid(revisionId)) continue;
    if (typeof name !== "string" || !name || typeof connector !== "string")
      continue;
    if (typeof enabled !== "boolean") continue;
    if (origin !== undefined && origin !== null && typeof origin !== "string")
      continue;
    items.push({
      connectionId,
      revisionId,
      name,
      connector,
      enabled,
      origin: typeof origin === "string" && origin ? origin : null,
    });
  }
  return items;
}

/** The environment's test policy, or null when the answer isn't one. */
export function readTestPolicy(value: unknown): EnvironmentTestPolicy | null {
  if (!isRecord(value)) return null;
  const { test_calls: calls, revision } = value;
  if (calls !== "enabled" && calls !== "disabled") return null;
  if (
    typeof revision !== "number" ||
    !Number.isInteger(revision) ||
    revision < 0
  )
    return null;
  return { test_calls: calls, revision };
}

function readTest(value: unknown): ConnectionTest {
  const record = isRecord(value) ? value : {};
  const reason = record["reason"];
  const encrypted = record["encrypted"];
  return {
    ok: record["ok"] === true,
    reason:
      typeof reason === "string" && REASONS.has(reason)
        ? (reason as TestReason)
        : null,
    encrypted: typeof encrypted === "boolean" ? encrypted : null,
    message: null,
  };
}

/** Refusals that answer the question "can this connection be used?": the platform says why. */
const ANSWERS = new Set([403, 404, 409, 422]);

/**
 * The platform's connection operations in one environment. `environment` is
 * the environment's API path when the caller made it; requests keep going
 * there even after Studio switched to another one.
 */
export function platformConnections(
  api: StudioApi,
  environment?: string,
): ConnectionsApi {
  const scope = () => environment ?? api.environment;
  return {
    async bindable(connector) {
      const base = `${scope()}/bindable-connections?connector=${encodeURIComponent(connector)}&limit=${PAGE}`;
      const items: BindableItem[] = [];
      let cursor: string | null = null;
      for (let page = 0; page < MAX_PAGES; page++) {
        const answer: unknown = await api.request(
          cursor ? `${base}&cursor=${encodeURIComponent(cursor)}` : base,
        );
        items.push(...readBindable(answer));
        const next = isRecord(answer) ? answer["next_cursor"] : null;
        if (typeof next !== "string" || !next) return items;
        cursor = next;
      }
      throw Error(
        "Too many connections to list here. Ask an administrator to narrow the environment's connections.",
      );
    },
    async test(revisionId) {
      try {
        return readTest(
          await api.request(
            `${scope()}/connections/${encodeURIComponent(revisionId)}/test`,
            "POST",
          ),
        );
      } catch (error) {
        if (error instanceof ApiError && ANSWERS.has(error.status))
          return {
            ok: false,
            reason: null,
            encrypted: null,
            message: error.message,
          };
        throw error;
      }
    },
    async policy() {
      let answer: unknown;
      try {
        answer = await api.request(`${scope()}/test-policy`);
      } catch (error) {
        if (error instanceof ApiError && error.status === 403)
          return "unreadable";
        throw error;
      }
      const policy = readTestPolicy(answer);
      if (!policy) throw Error("The platform's test policy couldn't be read.");
      return policy;
    },
  };
}
