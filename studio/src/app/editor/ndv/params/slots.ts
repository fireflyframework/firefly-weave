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
// Workflow slots are declarations; live connections are selected when activating.
import type { Step, Workflow } from "../../../model";
import { allSteps as walkSteps } from "../step-walk";
import { uniqueIdentifier } from "./identifiers";
export interface Slot {
  name: string;
  connector: string;
  required: boolean;
}
export const SLOT_HINT =
  "Choose the real connection when you activate, or connect to a platform to test.";
export const NO_MANAGE =
  "Your account can't manage connections here. Ask an administrator.";
const isObject = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
export function slotsOf(workflow: Workflow): Slot[] {
  const connections = isObject(workflow.spec["connections"])
    ? workflow.spec["connections"]
    : {};
  return Object.entries(connections).map(([name, value]) => ({
    name,
    connector:
      isObject(value) && typeof value["connector"] === "string"
        ? value["connector"]
        : "",
    required: !isObject(value) || value["required"] !== false,
  }));
}
export function slotBase(connector: string): string {
  return connector.split("@")[0].replace(/^weave-/, "") || "connection";
}
export const newSlotName = (workflow: Workflow, connector: string): string =>
  uniqueIdentifier(
    slotBase(connector),
    new Set(slotsOf(workflow).map((slot) => slot.name)),
  );
export function renamedConnections(
  connections: Record<string, unknown>,
  from: string,
  to: string,
): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(connections).map(([name, value]) => [
      name === from ? to : name,
      value,
    ]),
  );
}
/** Only the language's author step lists, never literal data named steps. */
export function allSteps(definition: unknown): Step[] {
  const spec =
    isObject(definition) && isObject(definition["spec"])
      ? definition["spec"]
      : null;
  return spec && Array.isArray(spec["steps"])
    ? walkSteps(spec["steps"].filter(isObject) as Step[])
    : [];
}
