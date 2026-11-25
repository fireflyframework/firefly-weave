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
// A transaction's forms: its result in Parameters; in Settings, its business
// key and how long it holds reservations, retries its undo steps and goes on
// trying. The platform fills a policy in once one is there, so Studio writes
// it whole when one value differs from its default, and leaves it out while
// every value is its default.
import { jsonEqual } from "../../../../forms/core/json";
import type { Step } from "../../../../model";
import type { Edit, FieldSpec, FormSpec, Json } from "../../registry";
import { isRecord } from "../shared";
import { BUSINESS_KEY_SCHEMA, durationParam } from "./shared";

/** The platform's defaults (contracts/transactions.py), in the order it writes them. */
const POLICY = {
  holdSeconds: 900,
  undoRetry: { maxAttempts: 10, initialDelaySeconds: 2, maxDelaySeconds: 300 },
  undoTimeoutSeconds: 86_400,
};

export const transactionFields = (): FieldSpec[] => [
  { path: ["output"], label: "Result" },
  {
    path: ["businessKey"],
    label: "Business key",
    templateCapable: true,
    expectedSchema: () => BUSINESS_KEY_SCHEMA,
  },
];

export const transactionForm = (): FormSpec => ({
  fields: [
    {
      id: "output",
      path: ["output"],
      type: "json",
      label: "Result",
      mapping: "both",
      // The language needs a result: cleared, it is null again.
      default: null,
      whenRemoved: "default",
      emptyMapped: true,
      placeholder: "Empty (null)",
      hint: "What later steps read as this transaction's output.",
    },
  ],
});

/** The wait between tries as the step has it; the longest wait is at least as long. */
function firstWait(step: Step): number {
  const policy = isRecord(step["policy"]) ? step["policy"] : {};
  const retry = isRecord(policy["undoRetry"]) ? policy["undoRetry"] : {};
  const wait = retry["initialDelaySeconds"];
  return typeof wait === "number" ? wait : POLICY.undoRetry.initialDelaySeconds;
}

export const transactionSettings = (): FormSpec => ({
  fields: [
    {
      id: "businessKey",
      path: ["businessKey"],
      type: "text",
      label: "Business key",
      typeTag: "Optional",
      mapping: "both",
      emptyMapped: true,
      placeholder: "Map a reference of your own",
      templateCapable: true,
      maxLength: 200,
      hint: "Shown in Operations and in platform events. Use a reference, never personal data.",
    },
    durationParam(
      "holdSeconds",
      ["policy", "holdSeconds"],
      "Hold reservations for",
      {
        min: 30,
        max: 86_400,
        default: POLICY.holdSeconds,
        hint: "From 30 seconds to 1 day. Steps that reserve hold for this long unless they set their own Hold for.",
      },
    ),
    {
      id: "undoRetry",
      path: ["policy", "undoRetry"],
      type: "fields",
      label: "Undo retries",
      default: POLICY.undoRetry,
      expandChildren: true,
      hint: "Weave retries each undo step until it succeeds, waiting longer each time.",
      children: (step) => [
        {
          id: "undoRetry.maxAttempts",
          path: ["policy", "undoRetry", "maxAttempts"],
          type: "number",
          label: "Max tries",
          min: 1,
          default: POLICY.undoRetry.maxAttempts,
        },
        durationParam(
          "undoRetry.initialDelaySeconds",
          ["policy", "undoRetry", "initialDelaySeconds"],
          "Wait between tries",
          { default: POLICY.undoRetry.initialDelaySeconds },
        ),
        durationParam(
          "undoRetry.maxDelaySeconds",
          ["policy", "undoRetry", "maxDelaySeconds"],
          "Longest wait",
          {
            min: firstWait(step),
            default: POLICY.undoRetry.maxDelaySeconds,
          },
        ),
      ],
    },
    durationParam(
      "undoTimeout",
      ["policy", "undoTimeoutSeconds"],
      "Give up undoing after",
      {
        min: 60,
        max: 604_800,
        default: POLICY.undoTimeoutSeconds,
        hint: "If an undo, a release or a confirm still hasn't finished after this long, Weave stops trying and the transaction is in doubt.",
      },
    ),
  ],
});

/**
 * What a change of one policy value makes of the whole policy: every value,
 * its default where the change left none; nothing while all are defaults.
 * Null for a step without a policy to settle.
 */
export function settledPolicy(step: Step): Edit | null {
  const policy = step["policy"];
  if ((step.kind as string) !== "transaction" || !isRecord(policy)) return null;
  const retry = policy["undoRetry"];
  const whole = {
    ...POLICY,
    ...policy,
    undoRetry: { ...POLICY.undoRetry, ...(isRecord(retry) ? retry : {}) },
  } as unknown as Json;
  return {
    path: ["policy"],
    value: jsonEqual(whole, POLICY) ? undefined : whole,
  };
}
