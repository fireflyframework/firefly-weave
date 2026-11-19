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
// Test activations of the saved draft in the selected environment: create
// one, start runs on it (marked Test), listen for a test event on a test URL,
// and retire it. The platform's refusals come back as typed outcomes so the
// editor can ask for a confirmation, wait, or explain what to fix.
import { ApiError, type Diagnostic, type StudioApi } from "../../api";
import type { Json } from "../../forms/core/json";
import type {
  DraftTestActivation,
  DraftTestActivationRequest,
  DraftTestListen,
  DraftTestListenRequest,
  SideEffect,
} from "./execution";

/** What this client needs from `StudioApi`. */
export type TestActivationApi = Pick<StudioApi, "environment" | "mutate">;

/** A run a test activation started, as the platform answers it. */
export interface TestRun {
  id: string;
  status?: string;
  [key: string]: unknown;
}

export type TestRefusal =
  | { kind: "disabled"; message: string }
  | {
      kind: "confirm";
      sideEffect: SideEffect;
      steps: string[];
      message: string;
    }
  | { kind: "expired"; message: string }
  | { kind: "compile"; diagnostics: Diagnostic[]; message: string }
  | { kind: "lifetime"; message: string }
  | { kind: "busy"; retryAfter?: number; message: string }
  | { kind: "connection"; slots: Record<string, string>; message: string }
  | { kind: "forbidden"; message: string }
  | { kind: "missing"; message: string }
  | { kind: "failed"; status: number; code: string; message: string };
export type TestOutcome<T> =
  | { ok: true; value: T }
  | { ok: false; refusal: TestRefusal };

const SIDE_EFFECTS: readonly SideEffect[] = [
  "read_only",
  "idempotent",
  "idempotency_key",
  "non_idempotent",
];

const record = (value: unknown): Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};

function refusal(error: ApiError): TestRefusal {
  const result = record(record(error.detail)["result"]);
  const message = error.message;
  switch (error.code) {
    case "WV-STEPTEST-DISABLED":
      return { kind: "disabled", message };
    case "WV-STEPTEST-CONFIRM": {
      const effect = result["side_effect"];
      const steps = result["steps"];
      return {
        kind: "confirm",
        // An effect Studio can't read is confirmed as the strongest one.
        sideEffect: SIDE_EFFECTS.includes(effect as SideEffect)
          ? (effect as SideEffect)
          : "non_idempotent",
        steps: Array.isArray(steps)
          ? steps.filter((step): step is string => typeof step === "string")
          : [],
        message,
      };
    }
    case "WV-TESTACTIVATION-EXPIRED":
      return { kind: "expired", message };
    case "WV-TESTACTIVATION-COMPILE": {
      const diagnostics = result["diagnostics"];
      return {
        kind: "compile",
        diagnostics: Array.isArray(diagnostics)
          ? (diagnostics as Diagnostic[])
          : [],
        message,
      };
    }
    case "WV-TESTACTIVATION-LIFETIME":
      return { kind: "lifetime", message };
    case "WV-STEPTEST-BUSY":
      return error.retryAfter === undefined
        ? { kind: "busy", message }
        : { kind: "busy", retryAfter: error.retryAfter, message };
    case "WV-STEPTEST-CONNECTION": {
      const slots: Record<string, string> = {};
      for (const [slot, words] of Object.entries(record(result["slots"])))
        if (typeof words === "string") slots[slot] = words;
      return { kind: "connection", slots, message };
    }
  }
  if (error.status === 403) return { kind: "forbidden", message };
  if (error.status === 404) return { kind: "missing", message };
  return { kind: "failed", status: error.status, code: error.code, message };
}

async function outcome<T>(send: () => Promise<T>): Promise<TestOutcome<T>> {
  try {
    return { ok: true, value: await send() };
  } catch (error) {
    if (error instanceof ApiError)
      return { ok: false, refusal: refusal(error) };
    throw error;
  }
}

const base = (api: TestActivationApi, id?: string) =>
  `${api.environment}/test-activations${id === undefined ? "" : `/${encodeURIComponent(id)}`}`;

/** `POST {ENV}/test-activations`: a test activation of the saved draft. */
export function createTestActivation(
  api: TestActivationApi,
  request: DraftTestActivationRequest,
): Promise<TestOutcome<DraftTestActivation>> {
  return outcome(() =>
    api.mutate<DraftTestActivation>(base(api), "POST", request),
  );
}

/** `POST {ENV}/test-activations/{id}/runs`: a test run with `input`; the same `key` never starts a second run. */
export function runTestActivation(
  api: TestActivationApi,
  id: string,
  input: Json,
  key?: string,
): Promise<TestOutcome<TestRun>> {
  return outcome(() =>
    api.mutate<TestRun>(
      `${base(api, id)}/runs`,
      "POST",
      { input },
      undefined,
      key,
    ),
  );
}

/** `POST {ENV}/test-activations/{id}/listen`: a test URL that starts a test run per signed request. */
export function listenForTestEvent(
  api: TestActivationApi,
  id: string,
  request: DraftTestListenRequest,
): Promise<TestOutcome<DraftTestListen>> {
  return outcome(() =>
    api.mutate<DraftTestListen>(`${base(api, id)}/listen`, "POST", request),
  );
}

/** `DELETE {ENV}/test-activations/{id}`: no more runs or test events on it. */
export function retireTestActivation(
  api: TestActivationApi,
  id: string,
): Promise<TestOutcome<null>> {
  return outcome(async () => {
    await api.mutate<unknown>(base(api, id), "DELETE", undefined);
    return null;
  });
}
