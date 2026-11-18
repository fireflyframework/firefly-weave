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
// Which connection each of a workflow's slots uses in each environment, kept on
// this computer only (never in the definition): the platform decides what a
// person may bind; this only remembers the choice. One document under the
// account's prefix holds IDs, names and the last test result, nothing else.
import { signal } from "@angular/core";
import { storeKey, type StoreBackend } from "../state/browser-store";

export const DEV_BINDINGS_NAME = "weave.devBindings.v1";
export const DEV_BINDINGS_MAX_BYTES = 65_536;
export const DEV_BINDINGS_MAX_ENTRIES = 500;

export interface DevBinding {
  connectionId: string;
  revisionId: string;
  name: string;
  tested?: { ok: boolean; at: string };
}
export interface DevBindingEntry extends DevBinding {
  environmentId: string;
  workflow: string;
  slot: string;
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;
const MAX_NAME = 253;
const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const isName = (value: unknown): value is string =>
  typeof value === "string" && value.length <= MAX_NAME && NAME.test(value);
const isUuid = (value: unknown): value is string =>
  typeof value === "string" && UUID.test(value);

/** A binding with its typed fields only; null when any of them is wrong. */
function binding(value: Record<string, unknown>): DevBinding | null {
  const { connectionId, revisionId, name, tested } = value;
  if (!isUuid(connectionId) || !isUuid(revisionId)) return null;
  if (typeof name !== "string" || !name || name.length > MAX_NAME) return null;
  const clean: DevBinding = { connectionId, revisionId, name };
  if (tested === undefined) return clean;
  if (
    !isRecord(tested) ||
    typeof tested["ok"] !== "boolean" ||
    typeof tested["at"] !== "string" ||
    Number.isNaN(Date.parse(tested["at"]))
  )
    return null;
  return { ...clean, tested: { ok: tested["ok"], at: tested["at"] } };
}

// The kept document is compact so that 500 bindings fit in 64 KiB:
// {"v":1,"bindings":{<environment ID>:{<workflow>:{<slot>:[connectionId, revisionId, name, ok?, at?]}}}}.
type Row = [string, string, string] | [string, string, string, boolean, string];

/** The kept document's text for these entries. */
export function writeDevBindings(entries: readonly DevBindingEntry[]): string {
  const tree: Record<string, Record<string, Record<string, Row>>> = {};
  for (const entry of entries) {
    const row: Row = entry.tested
      ? [
          entry.connectionId,
          entry.revisionId,
          entry.name,
          entry.tested.ok,
          entry.tested.at,
        ]
      : [entry.connectionId, entry.revisionId, entry.name];
    ((tree[entry.environmentId] ??= {})[entry.workflow] ??= {})[entry.slot] =
      row;
  }
  return JSON.stringify({ v: 1, bindings: tree });
}

/** The kept document's entries, typed field by field; anything malformed or oversized reads as empty. */
export function readDevBindings(text: string | null): DevBindingEntry[] {
  if (text === null || text.length > DEV_BINDINGS_MAX_BYTES) return [];
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    return [];
  }
  if (
    !isRecord(value) ||
    Object.keys(value).length !== 2 ||
    value["v"] !== 1 ||
    !isRecord(value["bindings"])
  )
    return [];
  const entries: DevBindingEntry[] = [];
  for (const [environmentId, workflows] of Object.entries(value["bindings"])) {
    if (!isUuid(environmentId) || !isRecord(workflows)) return [];
    for (const [workflow, slots] of Object.entries(workflows)) {
      if (!isName(workflow) || !isRecord(slots)) return [];
      for (const [slot, row] of Object.entries(slots)) {
        if (!isName(slot) || !Array.isArray(row)) return [];
        if (row.length !== 3 && row.length !== 5) return [];
        const [connectionId, revisionId, name, ok, at] = row as unknown[];
        const kept = binding(
          row.length === 5
            ? { connectionId, revisionId, name, tested: { ok, at } }
            : { connectionId, revisionId, name },
        );
        if (!kept) return [];
        entries.push({ environmentId, workflow, slot, ...kept });
        if (entries.length > DEV_BINDINGS_MAX_ENTRIES) return [];
      }
    }
  }
  return entries;
}

/**
 * A binding follows its connection: the bindable list's current revision and
 * name replace the kept ones, and a new revision forgets the last test result.
 */
export function followRevision(
  kept: DevBinding,
  current: { revisionId: string; name: string },
): DevBinding {
  if (kept.revisionId === current.revisionId && kept.name === current.name)
    return kept;
  if (kept.revisionId !== current.revisionId)
    return {
      connectionId: kept.connectionId,
      revisionId: current.revisionId,
      name: current.name,
    };
  return { ...kept, name: current.name };
}

const keyOf = (environmentId: string, workflow: string, slot: string) =>
  `${environmentId.toLowerCase()}\u0000${workflow}\u0000${slot}`;

export class DevBindings {
  private entries = new Map<string, DevBindingEntry>();
  private readonly changed = signal(0);
  /** Changes whenever a binding is set, cleared or moved. */
  readonly version = this.changed.asReadonly();

  constructor(
    private readonly backend: StoreBackend | null,
    /** The account's storage prefix; null keeps bindings in memory for this session only. */
    readonly prefix: string | null,
  ) {}

  private get key(): string | null {
    return this.prefix === null
      ? null
      : storeKey(this.prefix, DEV_BINDINGS_NAME);
  }

  async load(): Promise<void> {
    const key = this.key;
    if (!key || !this.backend) return;
    const kept = readDevBindings(await this.backend.get(key));
    this.entries = new Map(
      kept.map((entry) => [
        keyOf(entry.environmentId, entry.workflow, entry.slot),
        entry,
      ]),
    );
    this.changed.update((n) => n + 1);
  }

  get(
    environmentId: string,
    workflow: string,
    slot: string,
  ): DevBinding | null {
    const entry = this.entries.get(keyOf(environmentId, workflow, slot));
    if (!entry) return null;
    const { connectionId, revisionId, name, tested } = entry;
    return tested
      ? { connectionId, revisionId, name, tested }
      : { connectionId, revisionId, name };
  }

  /**
   * Keeps (or with null forgets) a slot's binding. False when the binding is
   * malformed, the limits would be passed, or nothing could be kept on this
   * computer; a refused change leaves what was there.
   */
  async set(
    environmentId: string,
    workflow: string,
    slot: string,
    value: DevBinding | null,
  ): Promise<boolean> {
    if (!isUuid(environmentId) || !isName(workflow) || !isName(slot))
      return false;
    const next = new Map(this.entries);
    const key = keyOf(environmentId, workflow, slot);
    if (value === null) next.delete(key);
    else {
      const clean = isRecord(value)
        ? binding(value as unknown as Record<string, unknown>)
        : null;
      if (!clean) return false;
      next.set(key, { environmentId, workflow, slot, ...clean });
    }
    return this.commit(next);
  }

  /** Moves a slot's binding in one environment when the slot is renamed. */
  async renameSlot(
    environmentId: string,
    workflow: string,
    from: string,
    to: string,
  ): Promise<boolean> {
    const kept = this.entries.get(keyOf(environmentId, workflow, from));
    if (!kept || !isName(to)) return false;
    const next = new Map(this.entries);
    next.delete(keyOf(environmentId, workflow, from));
    next.set(keyOf(environmentId, workflow, to), { ...kept, slot: to });
    return this.commit(next);
  }

  private async commit(next: Map<string, DevBindingEntry>): Promise<boolean> {
    if (next.size > DEV_BINDINGS_MAX_ENTRIES) return false;
    const text = writeDevBindings([...next.values()]);
    if (text.length > DEV_BINDINGS_MAX_BYTES) return false;
    const key = this.key;
    if (!key || !this.backend) {
      this.entries = next;
      this.changed.update((n) => n + 1);
      return false;
    }
    if (!(await this.backend.set(key, text))) return false;
    this.entries = next;
    this.changed.update((n) => n + 1);
    return true;
  }
}
