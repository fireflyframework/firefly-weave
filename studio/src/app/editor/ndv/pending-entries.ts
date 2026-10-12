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
// What a person typed in step details that isn't in the workflow yet: a
// number below its minimum or not a number, JSON or a template that doesn't
// parse, a note the canvas has no room for. The workflow keeps the last
// value that applied; the entry stays on screen with its message through
// closing and reopening step details, moving to another step and switching
// tabs, for as long as the workflow stays open. Each entry belongs to one
// field, described as it was, over the value it was typed on: when either
// changes, the entry goes. Nothing here is stored anywhere else.

export type PendingKind = "number" | "json" | "template" | "note";
export interface PendingEntry {
  kind: PendingKind;
  /** What is on screen. */
  text: string;
  /** Why it doesn't apply. */
  message: string;
  /** The field's label, for the issues list. */
  label: string;
}
/** What an entry belongs to; all three must still match to show it. */
export interface PendingOwner {
  /** The field as it was described. */
  descriptor: string;
  /** The stored value it was typed over. */
  baseline: string;
  /** The rows around it (their counts), so a row's entry never moves to another row. */
  structure: string;
}
interface Held extends PendingEntry, PendingOwner {
  target: string;
  key: string;
}

const id = (target: string, key: string) => `${target}\n${key}`;
const owns = (held: Held, owner: PendingOwner) =>
  held.descriptor === owner.descriptor &&
  held.baseline === owner.baseline &&
  held.structure === owner.structure;

export class PendingEntries {
  private readonly entries = new Map<string, Held>();
  /** Changes on every hold, release and drop, so views that list entries know. */
  version = 0;

  hold(
    target: string,
    key: string,
    owner: PendingOwner,
    entry: PendingEntry,
  ): void {
    this.entries.set(id(target, key), { ...entry, ...owner, target, key });
    this.version++;
  }
  /** The entry, while it still belongs to this field and value; a stale one goes. */
  get(target: string, key: string, owner: PendingOwner): PendingEntry | null {
    const held = this.entries.get(id(target, key));
    if (!held) return null;
    if (owns(held, owner)) return held;
    this.release(target, key);
    return null;
  }
  get size(): number {
    return this.entries.size;
  }
  has(target: string, key: string): boolean {
    return this.entries.has(id(target, key));
  }
  release(target: string, key: string): void {
    if (this.entries.delete(id(target, key))) this.version++;
  }
  /** The keys held for a step. */
  keys(target: string): string[] {
    return [...this.entries.values()]
      .filter((held) => held.target === target)
      .map((held) => held.key);
  }
  /** A rename made in step details: the step's entries follow it. */
  rename(from: string, to: string): void {
    if (from === to) return;
    for (const held of [...this.entries.values()])
      if (held.target === from) {
        this.entries.delete(id(from, held.key));
        this.entries.set(id(to, held.key), { ...held, target: to });
        this.version++;
      }
  }
  /** Drops the entries of steps that are gone. */
  prune(keep: (target: string) => boolean): void {
    for (const held of [...this.entries.values()])
      if (!keep(held.target)) this.release(held.target, held.key);
  }
  clear(): void {
    if (!this.entries.size) return;
    this.entries.clear();
    this.version++;
  }
}
