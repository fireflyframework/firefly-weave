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
// What can happen to one workflow's test data, as pure functions: set or
// clear the test input and pinned output, look a pin up for one iteration,
// follow a renamed step, drop the entries of deleted steps, and tell data
// recorded from a run apart. Every function returns a new document (or the
// same one when nothing changed) and stamps `updatedAt`.
import type { Json } from "../ndv/registry";
import { instanceStep, renameInstance } from "./instance-keys";
import {
  TEST_DATA_MAX_BYTES,
  type DataSource,
  type HumanEntry,
  type PinEntry,
  type Provenance,
  type SignalEntry,
  type StudioTestData,
} from "./test-data";

export const TEST_DATA_TOO_LARGE =
  "Test data is limited to 1 MB. Unpin some output to make room.";
type Keyed = "pins" | "signals" | "humans" | "aiTurns";
const KEYED: readonly Keyed[] = ["pins", "signals", "humans", "aiTurns"];

export function emptyTestData(now: string): StudioTestData {
  return { schemaVersion: 1, kind: "weave.studio/testData", updatedAt: now };
}
const stamped = (data: StudioTestData, now: string): StudioTestData => ({
  ...data,
  updatedAt: now,
});
/** The document without an empty section, so an emptied map is left out as the format wants. */
function withSection<K extends Keyed>(
  data: StudioTestData,
  section: K,
  value: NonNullable<StudioTestData[K]>,
): StudioTestData {
  const next = { ...data };
  if (Object.keys(value).length) next[section] = value;
  else delete next[section];
  return next;
}

export function withInput(
  data: StudioTestData,
  value: Json,
  source: DataSource,
  now: string,
): StudioTestData {
  return stamped({ ...data, input: { value, source } }, now);
}
export function withoutInput(
  data: StudioTestData,
  now: string,
): StudioTestData {
  if (!data.input) return data;
  const next = { ...data };
  delete next.input;
  return stamped(next, now);
}

/** The pin for one iteration: its own key first, then its step's. */
export function pinOf(
  data: StudioTestData,
  key: string,
): { key: string; entry: PinEntry } | null {
  const pins = data.pins ?? {};
  for (const candidate of [key, instanceStep(key)])
    if (Object.hasOwn(pins, candidate))
      return { key: candidate, entry: pins[candidate] };
  return null;
}
export function withPin(
  data: StudioTestData,
  key: string,
  entry: PinEntry,
  now: string,
): StudioTestData {
  return stamped(
    { ...data, pins: { ...(data.pins ?? {}), [key]: entry } },
    now,
  );
}
/** Sets (or, with null, removes) the test answer of a signal or a human task. */
export function withScript(
  data: StudioTestData,
  section: "signals" | "humans",
  key: string,
  entry: SignalEntry | HumanEntry | null,
  now: string,
): StudioTestData {
  const current = { ...(data[section] ?? {}) } as Record<string, unknown>;
  if (entry === null) {
    if (!Object.hasOwn(current, key)) return data;
    delete current[key];
  } else current[key] = entry;
  return stamped(
    withSection(
      data,
      section,
      current as NonNullable<StudioTestData["signals"]>,
    ),
    now,
  );
}
export function withoutPin(
  data: StudioTestData,
  key: string,
  now: string,
): StudioTestData {
  if (!data.pins || !Object.hasOwn(data.pins, key)) return data;
  const pins = { ...data.pins };
  delete pins[key];
  return stamped(withSection(data, "pins", pins), now);
}

/** An entry's frame: the sub-workflow it applies in, or "" for this workflow. */
const frameOf = (entry: unknown): string =>
  typeof entry === "object" &&
  entry !== null &&
  typeof (entry as { frame?: unknown }).frame === "string"
    ? (entry as { frame: string }).frame
    : "";
/** The step of this workflow that an entry belongs to: its own, or the call that opens its frame. */
export function ownerOf(key: string, entry: unknown): string {
  const frame = frameOf(entry);
  return instanceStep(frame ? frame.split("/")[0] : key);
}

/**
 * A step was renamed: keys of its entries and the first part of frames that
 * start at it follow. Entries inside a frame belong to the called workflow,
 * so their own keys stay.
 */
export function renameEntries(
  data: StudioTestData,
  from: string,
  to: string,
  now: string,
): StudioTestData {
  let changed = false;
  let next = data;
  for (const section of KEYED) {
    const entries = data[section] as Record<string, unknown> | undefined;
    if (!entries) continue;
    const renamed: Record<string, unknown> = {};
    for (const [key, entry] of Object.entries(entries)) {
      const frame = frameOf(entry);
      if (frame) {
        const [first, ...rest] = frame.split("/");
        const moved = [renameInstance(first, from, to), ...rest].join("/");
        if (moved !== frame) changed = true;
        renamed[key] =
          moved === frame ? entry : { ...(entry as object), frame: moved };
        continue;
      }
      const moved = renameInstance(key, from, to);
      if (moved !== key) changed = true;
      renamed[moved] = entry;
    }
    next = { ...next, [section]: renamed };
  }
  return changed ? stamped(next, now) : data;
}

/** Drops the entries of steps that are gone; says which keys went (for the Undo toast). */
export function pruneEntries(
  data: StudioTestData,
  exists: (stepId: string) => boolean,
  now: string,
): { data: StudioTestData; removed: string[] } {
  const removed: string[] = [];
  let next = data;
  for (const section of KEYED) {
    const entries = data[section] as Record<string, unknown> | undefined;
    if (!entries) continue;
    const kept = Object.fromEntries(
      Object.entries(entries).filter(([key, entry]) => {
        const keep = exists(ownerOf(key, entry));
        if (!keep) removed.push(key);
        return keep;
      }),
    );
    if (Object.keys(kept).length !== Object.keys(entries).length)
      next = withSection(next, section, kept as never);
  }
  return removed.length
    ? { data: stamped(next, now), removed }
    : { data, removed };
}

/** Data recorded from a run or a test call: it names that run and its environment. */
export function runDerived(entry: unknown): boolean {
  if (typeof entry !== "object" || entry === null) return false;
  const { source, runId } = entry as Provenance & { source?: unknown };
  return source === "run" || source === "test-call" || runId !== undefined;
}
/** Every entry of the document, with its pointer, for checks and run data. */
export function entriesOf(
  data: StudioTestData,
): { path: string; entry: unknown }[] {
  const escape = (key: string) => key.replace(/~/g, "~0").replace(/\//g, "~1");
  const out: { path: string; entry: unknown }[] = [];
  if (data.input) out.push({ path: "/input", entry: data.input });
  for (const section of KEYED)
    for (const [key, entry] of Object.entries(data[section] ?? {}))
      out.push({ path: `/${section}/${escape(key)}`, entry });
  for (const [reference, callee] of Object.entries(data.workflows ?? {}))
    for (const [key, entry] of Object.entries(callee.mocks))
      out.push({
        path: `/workflows/${escape(reference)}/mocks/${escape(key)}`,
        entry,
      });
  return out;
}
export function containsRunData(data: StudioTestData): boolean {
  return entriesOf(data).some(({ entry }) => runDerived(entry));
}
/** The document without anything recorded from a run (for Save without run data). */
export function withoutRunData(
  data: StudioTestData,
  now: string,
): StudioTestData {
  if (!containsRunData(data)) return data;
  let next: StudioTestData = { ...data };
  if (next.input && runDerived(next.input)) delete next.input;
  for (const section of KEYED) {
    const entries = next[section] as Record<string, unknown> | undefined;
    if (!entries) continue;
    next = withSection(
      next,
      section,
      Object.fromEntries(
        Object.entries(entries).filter(([, entry]) => !runDerived(entry)),
      ) as never,
    );
  }
  if (next.workflows) {
    const workflows = Object.fromEntries(
      Object.entries(next.workflows).map(([reference, callee]) => [
        reference,
        {
          mocks: Object.fromEntries(
            Object.entries(callee.mocks).filter(
              ([, entry]) => !runDerived(entry),
            ),
          ),
        },
      ]),
    );
    next = { ...next, workflows };
  }
  return stamped(next, now);
}

/** The document's size as the platform counts it: UTF-8 bytes of its JSON. */
export function testDataBytes(data: StudioTestData): number {
  return new TextEncoder().encode(JSON.stringify(data)).length;
}
export const fitsTestData = (data: StudioTestData): boolean =>
  testDataBytes(data) <= TEST_DATA_MAX_BYTES;
