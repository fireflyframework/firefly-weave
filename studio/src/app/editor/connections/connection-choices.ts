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
// What a step's Connection field offers when Studio is connected to an
// environment: the connections the person may bind, which one a fresh step
// starts with, which slot a choice fills, and how the last test went. Pure,
// so every sentence and rule is tested without the form.
import type { Workflow } from "../../model";
import type { SelectOption } from "../../forms/ui/select";
import { relativeTime } from "../../format";
import { newSlotName, slotsOf } from "../ndv/params/slots";
import type { BindableItem } from "./connections-api";
import type { DevBinding } from "./dev-bindings";

/** A kept test result counts as recent for ten minutes; after that, choosing tests again. */
export const RECENT_TEST_MS = 600_000;
/** The option value of Create connection; no connection ID can look like it. */
export const CREATE_CONNECTION = "\u0000create";
const NONE = "\u0000none";
const JUST_NOW_MS = 60_000;

/** "weave-http" for "weave-http@2.0.0". */
const connectorName = (connector: string) => connector.split("@")[0];
const createsHere = (connector: string) =>
  connectorName(connector) === "weave-http";

export function connectionOptions(
  items: readonly BindableItem[],
  environment: string,
  can: { manage: boolean; connector: string },
): SelectOption[] {
  const group = `In ${environment}`;
  const options: SelectOption[] = items.map((item) =>
    item.enabled
      ? {
          value: item.connectionId,
          label: item.name,
          description: item.origin ?? "No address",
          group,
        }
      : {
          value: item.connectionId,
          label: item.name,
          description: `Turned off in ${environment}`,
          group,
          disabled: true,
        },
  );
  if (!options.length)
    options.push({
      value: NONE,
      label: `No ${connectorName(can.connector)} connections you can use in ${environment}.`,
      disabled: true,
    });
  const create: SelectOption = {
    value: CREATE_CONNECTION,
    label: "Create connection",
  };
  if (!createsHere(can.connector)) {
    create.description = `Studio creates weave-http connections here. Create ${connectorName(can.connector)} connections with weave platform integrations, or ask an administrator.`;
    create.disabled = true;
  } else if (!can.manage) {
    create.description = `Your account can't manage connections in ${environment}. Ask an administrator.`;
    create.disabled = true;
  }
  options.push(create);
  return options;
}

/** What a fresh step starts with: its slot's kept connection, else the only one there is, else nothing. */
export function preselection(
  items: readonly BindableItem[],
  binding: DevBinding | null,
): BindableItem | null {
  const usable = items.filter((item) => item.enabled);
  const kept =
    binding &&
    usable.find((item) => item.connectionId === binding.connectionId);
  if (kept) return kept;
  return usable.length === 1 ? usable[0] : null;
}

/**
 * The slot a chosen connection fills: the step's own slot when it already
 * names one of this connector, else the workflow's only slot for the
 * connector, else a new slot named after the connector.
 */
export function slotFor(
  workflow: Workflow,
  connector: string,
  current: string | null = null,
): { slot: string; declare: boolean } {
  const slots = slotsOf(workflow).filter(
    (slot) => slot.connector === connector,
  );
  if (current && slots.some((slot) => slot.name === current))
    return { slot: current, declare: false };
  if (slots.length === 1) return { slot: slots[0].name, declare: false };
  return { slot: newSlotName(workflow, connector), declare: true };
}

export interface TestPill {
  text: string;
  tone: "success" | "danger";
  /** A failed test offers Retry beside the pill. */
  retry: boolean;
}

export function testPill(
  tested: DevBinding["tested"],
  now: Date,
): TestPill | null {
  if (!tested) return null;
  if (!tested.ok) return { text: "Test failed", tone: "danger", retry: true };
  const age = now.getTime() - Date.parse(tested.at);
  return {
    text:
      age < JUST_NOW_MS
        ? "Tested just now"
        : `Tested ${relativeTime(tested.at, now)}`,
    tone: "success",
    retry: false,
  };
}

/**
 * Whether choosing or opening tests the connection: only for people who may
 * test it, and only when it wasn't tested in the last ten minutes. A failed
 * test is tested again by Retry, not on its own.
 */
export function needsTest(
  binding: DevBinding | null,
  canManage: boolean,
  now: Date,
): boolean {
  if (!binding || !canManage) return false;
  if (!binding.tested) return true;
  if (!binding.tested.ok) return false;
  return now.getTime() - Date.parse(binding.tested.at) > RECENT_TEST_MS;
}

/** "In local · slot crm · https://api.acme.test". */
export function fieldHint(
  environment: string,
  slot: string,
  origin: string | null,
): string {
  return [
    `In ${environment}`,
    `slot ${slot}`,
    ...(origin ? [origin] : []),
  ].join(" · ");
}

/** A plain-HTTP address: allowed, but marked "Not encrypted". */
export const notEncrypted = (origin: string | null): boolean =>
  !!origin && origin.startsWith("http:");

/** Why a kept binding can't be used any more, or null when it can. */
export function staleBinding(
  binding: DevBinding | null,
  items: readonly BindableItem[],
  environment: string,
): string | null {
  if (!binding) return null;
  const listed = items.find(
    (item) => item.connectionId === binding.connectionId,
  );
  if (!listed)
    return `${binding.name} isn't available in ${environment} anymore. Choose another connection.`;
  if (!listed.enabled)
    return `${listed.name} is turned off in ${environment}. Choose another connection.`;
  return null;
}
