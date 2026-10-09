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
// Small builders and lists the step forms share.
import { decode } from "../../../../forms/core/binding";
import type { ParamSpec, Path } from "../../registry";

export const DURATION_UNITS = ["seconds", "minutes", "hours", "days"] as const;

export const durationParam = (
  id: string,
  path: Path,
  label: string,
  extra: Partial<ParamSpec> = {},
): ParamSpec => ({
  id,
  path,
  type: "duration",
  label,
  units: [...DURATION_UNITS],
  min: 1,
  ...extra,
});

/** Kinds that can fail on their own; On error applies to them. */
export const ON_ERROR_KINDS: ReadonlySet<string> = new Set([
  "action",
  "llm",
  "agent",
  "transform",
  "decisionTable",
  "callWorkflow",
  "humanTask",
  "signal",
]);
/** Kinds whose failure always stops the run. */
export const STOP_ONLY_KINDS: ReadonlySet<string> = new Set([
  "switch",
  "parallel",
  "forEach",
]);

/** True when an input is one mapping (a reference or a formula), not fields. */
export function wholeMapping(expression: unknown): boolean {
  if (expression === undefined) return false;
  try {
    const kind = decode(expression).kind;
    return kind === "ref" || kind === "formula";
  } catch {
    return false;
  }
}
