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
// Starts one step of a saved draft for real in an environment, follows it and
// cancels it, through the platform's step test operations. One click is one
// idempotency key: an answer lost on the way back is asked for again with the
// same key, so the platform never starts a second test run for it. Every
// answer is read field by field; a refusal becomes one typed reason.
import { ApiError, type StudioApi } from "../../api";
import type {
  SideEffect,
  StepTestAccepted,
  StepTestOmission,
  StepTestPending,
  StepTestRequest,
  StepTestStatus,
  StepTestView,
} from "./execution";
import type { Json } from "../../forms/core/json";

export const POLL_FAST_MS = 1000;
export const POLL_FAST_FOR_MS = 30_000;
export const POLL_SLOW_MS = 2000;
export const FOLLOW_GRACE_MS = 60_000;
const BACKOFF_MAX_MS = 30_000;
const READ_FAILURES = 3;
const START_TIMEOUT_MS = 30_000;

export type StepTestRefusal =
  | {
      kind: "confirm";
      sideEffect: SideEffect;
      action: string;
      environment: string;
    }
  | {
      kind: "release";
      choices: Record<string, { id: string; label: string }[]>;
    }
  | { kind: "input"; diagnostics: { path: string; code: string }[] }
  | { kind: "secret"; paths: string[] }
  | { kind: "connection"; slots: Record<string, string> }
  | { kind: "disabled" }
  | { kind: "forbidden" }
  | { kind: "not-found" }
  | { kind: "kind"; message: string }
  | { kind: "busy"; retryAfterMs: number }
  | { kind: "account-changed" }
  | { kind: "offline" }
  | { kind: "lost" }
  | { kind: "failed"; message: string };

export interface StepTestClock {
  now(): number;
  /** Resolves after `ms`, or at once when `signal` aborts. */
  wait(ms: number, signal: AbortSignal): Promise<void>;
}
export const realClock: StepTestClock = {
  now: () => Date.now(),
  wait: (ms, signal) =>
    new Promise<void>((resolve) => {
      if (signal.aborted) return resolve();
      const timer = setTimeout(done, ms);
      function done() {
        clearTimeout(timer);
        signal.removeEventListener("abort", done);
        resolve();
      }
      signal.addEventListener("abort", done, { once: true });
    }),
};

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const SIDE_EFFECTS: ReadonlySet<string> = new Set([
  "read_only",
  "idempotent",
  "idempotency_key",
  "non_idempotent",
]);
const STATUSES: ReadonlySet<string> = new Set([
  "running",
  "waiting",
  "succeeded",
  "failed",
  "timed_out",
  "cancelled",
]);
const OMISSIONS: ReadonlySet<string> = new Set([
  "classified_secret",
  "classification_unavailable",
  "uncertain_derived",
  "confidential_text",
  "resource_limit",
]);
const PENDING_KINDS: ReadonlySet<string> = new Set([
  "confirm",
  "review",
  "person",
]);

const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const isUuid = (value: unknown): value is string =>
  typeof value === "string" && UUID.test(value);
const isSideEffect = (value: unknown): value is SideEffect =>
  typeof value === "string" && SIDE_EFFECTS.has(value);
const isString = (value: unknown): value is string => typeof value === "string";
const optional = <T>(value: unknown, is: (v: unknown) => v is T) =>
  value === undefined || is(value);

/** Whether a status is an end: the step test won't change any more. */
export const ended = (status: StepTestStatus): boolean =>
  status !== "running" && status !== "waiting";

function readPending(value: unknown): StepTestPending | null {
  if (!isRecord(value)) return null;
  const { node_id, instance_key, kind, tool, side_effect, task_id } = value;
  if (!isString(node_id) || !isString(instance_key)) return null;
  if (!isString(kind) || !PENDING_KINDS.has(kind)) return null;
  if (!optional(tool, isString) || !optional(task_id, isString)) return null;
  if (!optional(side_effect, isSideEffect)) return null;
  const pending: StepTestPending = {
    node_id,
    instance_key,
    kind: kind as StepTestPending["kind"],
  };
  if (tool !== undefined) pending.tool = tool;
  if (side_effect !== undefined) pending.side_effect = side_effect;
  if (task_id !== undefined) pending.task_id = task_id;
  return pending;
}

/**
 * A step test's view with every field typed, or null. Only an agent step can
 * wait for answers, so any other step's view with pending items is refused.
 */
export function readStepTestView(
  value: unknown,
  agent = false,
): StepTestView | null {
  if (!isRecord(value)) return null;
  const {
    id,
    run_id,
    status,
    side_effect,
    pending,
    output,
    omitted,
    error,
    attempts,
    started_at,
    ended_at,
    duration_ms,
  } = value;
  if (!isUuid(id) || !isUuid(run_id)) return null;
  if (!isString(status) || !STATUSES.has(status)) return null;
  if (!isSideEffect(side_effect) || !Array.isArray(pending)) return null;
  if (!agent && pending.length) return null;
  const items = pending.map(readPending);
  if (items.some((item) => item === null)) return null;
  if (omitted !== undefined) {
    if (!isString(omitted) || !OMISSIONS.has(omitted)) return null;
    if (output !== null && output !== undefined) return null;
  }
  let failure: StepTestView["error"] = null;
  if (error !== null && error !== undefined) {
    if (
      !isRecord(error) ||
      !isString(error["code"]) ||
      !isString(error["message"])
    )
      return null;
    failure = { code: error["code"], message: error["message"] };
  }
  if (
    typeof attempts !== "number" ||
    !Number.isInteger(attempts) ||
    attempts < 0
  )
    return null;
  if (!isString(started_at)) return null;
  if (ended_at !== null && !isString(ended_at)) return null;
  if (duration_ms !== null && typeof duration_ms !== "number") return null;
  const view: StepTestView = {
    id,
    run_id,
    status: status as StepTestStatus,
    side_effect,
    pending: items as StepTestPending[],
    output: (output ?? null) as Json,
    error: failure,
    attempts,
    started_at,
    ended_at,
    duration_ms,
  };
  if (omitted !== undefined) view.omitted = omitted as StepTestOmission;
  return view;
}

function readAccepted(value: unknown): StepTestAccepted | null {
  if (!isRecord(value)) return null;
  const { id, run_id, status, side_effect, expires_at } = value;
  if (!isUuid(id) || !isUuid(run_id) || status !== "running") return null;
  if (!isSideEffect(side_effect) || !isString(expires_at)) return null;
  return { id, run_id, status, side_effect, expires_at };
}

function readChoices(
  value: unknown,
): Record<string, { id: string; label: string }[]> | null {
  if (!isRecord(value)) return null;
  const choices: Record<string, { id: string; label: string }[]> = {};
  for (const [dependency, options] of Object.entries(value)) {
    if (!Array.isArray(options)) return null;
    const read: { id: string; label: string }[] = [];
    for (const option of options) {
      if (
        !isRecord(option) ||
        !isString(option["id"]) ||
        !isString(option["label"])
      )
        return null;
      read.push({ id: option["id"], label: option["label"] });
    }
    choices[dependency] = read;
  }
  return choices;
}

function readStrings(value: unknown): Record<string, string> | null {
  if (!isRecord(value)) return null;
  const read: Record<string, string> = {};
  for (const [key, text] of Object.entries(value)) {
    if (!isString(text)) return null;
    read[key] = text;
  }
  return read;
}

function typedRefusal(error: ApiError): StepTestRefusal | null {
  const detail = isRecord(error.detail) ? error.detail : {};
  const result = isRecord(detail["result"]) ? detail["result"] : {};
  switch (error.code) {
    case "WV-STEPTEST-CONFIRM": {
      const { side_effect, action, environment } = result;
      return isSideEffect(side_effect) &&
        isString(action) &&
        isString(environment)
        ? { kind: "confirm", sideEffect: side_effect, action, environment }
        : null;
    }
    case "WV-STEPTEST-RELEASE": {
      const choices = readChoices(result["choices"]);
      return choices ? { kind: "release", choices } : null;
    }
    case "WV-STEPTEST-INPUT": {
      const raw = result["diagnostics"];
      if (!Array.isArray(raw)) return null;
      const diagnostics: { path: string; code: string }[] = [];
      for (const item of raw) {
        if (
          !isRecord(item) ||
          !isString(item["path"]) ||
          !isString(item["code"])
        )
          return null;
        diagnostics.push({ path: item["path"], code: item["code"] });
      }
      return { kind: "input", diagnostics };
    }
    case "WV-SCHEMA-SECRET_VALUE": {
      const paths = result["paths"];
      return Array.isArray(paths) && paths.every(isString)
        ? { kind: "secret", paths: [...paths] }
        : null;
    }
    case "WV-STEPTEST-CONNECTION": {
      const slots = readStrings(result["slots"]);
      return slots ? { kind: "connection", slots } : null;
    }
    case "WV-STEPTEST-DISABLED":
      return { kind: "disabled" };
    case "WV-STEPTEST-KIND":
      return { kind: "kind", message: error.message };
    case "WV-STEPTEST-BUSY":
      return {
        kind: "busy",
        retryAfterMs: Math.max(1, error.retryAfter ?? 1) * 1000,
      };
    case "WV-PRINCIPAL-CHANGED":
      return { kind: "account-changed" };
    case "WV-STUDIO-OFFLINE":
      return { kind: "offline" };
    case "WV-STUDIO-UPSTREAM":
      return { kind: "lost" };
  }
  if (error.status === 403) return { kind: "forbidden" };
  if (error.status === 404) return { kind: "not-found" };
  return { kind: "failed", message: error.message };
}

/** Why a step test didn't start, from what the request threw. */
export function refusalOf(error: unknown): StepTestRefusal {
  if (error instanceof ApiError)
    return typedRefusal(error) ?? { kind: "failed", message: error.message };
  // fetch rejects with a TypeError when the Studio host can't be reached at all.
  if (error instanceof TypeError) return { kind: "offline" };
  if (error instanceof DOMException && error.name === "TimeoutError")
    return { kind: "lost" };
  return {
    kind: "failed",
    message: error instanceof Error ? error.message : String(error),
  };
}

export type StartAnswer =
  | { ok: true; accepted: StepTestAccepted }
  | { ok: false; refusal: StepTestRefusal };
export type FollowAnswer =
  | StepTestView
  | { stopped: "timeout" | "aborted" | "lost" };

export class StepTestClient {
  constructor(
    private readonly api: StudioApi,
    private readonly clock: StepTestClock = realClock,
  ) {}

  /**
   * Sends `POST {ENV}/step-tests` with the click's key. When the answer is
   * lost on the way back, `checking` is called and the same request is sent
   * once more with the same key: the platform answers with the step test the
   * first one started, or starts it if the first never arrived.
   */
  async start(
    body: StepTestRequest,
    key: string,
    checking?: () => void,
  ): Promise<StartAnswer> {
    const path = `${this.api.environment}/step-tests`;
    for (let attempt = 0; ; attempt++) {
      try {
        const accepted = readAccepted(
          await this.api.request(
            path,
            "POST",
            body,
            { "Idempotency-Key": key },
            START_TIMEOUT_MS,
          ),
        );
        return accepted
          ? { ok: true, accepted }
          : {
              ok: false,
              refusal: {
                kind: "failed",
                message: "The platform's answer couldn't be read.",
              },
            };
      } catch (error) {
        const refusal = refusalOf(error);
        if (refusal.kind !== "lost" || attempt > 0)
          return { ok: false, refusal };
        checking?.();
      }
    }
  }

  /**
   * Reads the step test every second for 30 seconds, then every 2 seconds,
   * until it ends, `signal` aborts, or `timeoutSeconds` plus a minute pass.
   * A read that fails waits twice as long (at most 30 s); three in a row give up.
   */
  async follow(
    id: string,
    timeoutSeconds: number,
    onView: (view: StepTestView) => void,
    signal: AbortSignal,
  ): Promise<FollowAnswer> {
    const path = `${this.api.environment}/step-tests/${encodeURIComponent(id)}`;
    const started = this.clock.now();
    const deadline = started + timeoutSeconds * 1000 + FOLLOW_GRACE_MS;
    let failures = 0;
    for (;;) {
      if (signal.aborted) return { stopped: "aborted" };
      if (this.clock.now() >= deadline) return { stopped: "timeout" };
      let view: StepTestView | null = null;
      try {
        view = readStepTestView(await this.api.request(path));
      } catch (error) {
        if (signal.aborted) return { stopped: "aborted" };
        if (
          error instanceof ApiError &&
          error.status >= 400 &&
          error.status < 500 &&
          error.status !== 429
        )
          return { stopped: "lost" };
      }
      if (signal.aborted) return { stopped: "aborted" };
      const interval =
        this.clock.now() - started < POLL_FAST_FOR_MS
          ? POLL_FAST_MS
          : POLL_SLOW_MS;
      if (view) {
        if (ended(view.status)) return view;
        failures = 0;
        onView(view);
        await this.clock.wait(interval, signal);
        continue;
      }
      if (++failures >= READ_FAILURES) return { stopped: "lost" };
      await this.clock.wait(
        Math.min(interval * 2 ** failures, BACKOFF_MAX_MS),
        signal,
      );
    }
  }

  /** Cancels the step test on the platform; its ended view, or null when it couldn't. */
  async cancel(id: string): Promise<StepTestView | null> {
    try {
      return readStepTestView(
        await this.api.request(
          `${this.api.environment}/step-tests/${encodeURIComponent(id)}/cancel`,
          "POST",
        ),
      );
    } catch {
      return null;
    }
  }
}
