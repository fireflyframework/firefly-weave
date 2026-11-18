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
// The item picked in each loop of one draft, which step details show, pin
// and execute inside a loop's body: one index per loop, the first item
// until another is picked. A pick holds while its loop and the Items of the
// loops around it stay as they were. How many items a loop has comes from
// the last Execute, else from evaluating its Items against the current data,
// else it isn't known. Answers that arrive after an edit or a new pick are
// dropped.
import { signal } from "@angular/core";
import {
  canonicalJson,
  getAt,
  isJsonObject,
  parsePointer,
} from "../../forms/core/json";
import { enclosingLoops, stepPointerOf } from "../formula/loops";
import { rewriteStepRefs } from "../../model";
import type { InstanceKey } from "../ndv/registry";
import type { LoopProgress } from "./execution";
import { instanceStep, isInstanceKey, renameInstance } from "./instance-keys";

/** The item picked in one loop; `index` counts from 0. */
export interface LoopPick {
  loopId: string;
  index: number;
}
export interface LoopCount {
  count: number;
  /** False until Execute or the current data says how many items there are. */
  known: boolean;
}
/** What the picks read from the editor. */
export interface IterationsHost {
  /** The draft's workflow now. */
  definition(): unknown;
  /** Changes with every edit of the draft. */
  revision(): number;
  /** The loop's Items evaluated against the current data, with these picks around it; null when Studio can't. */
  items(loopId: string, picks: readonly LoopPick[]): Promise<unknown>;
}

const UNKNOWN: LoopCount = { count: 0, known: false };
/** Counts kept from the current data, at most; the oldest go first. */
const MAX_EVALUATED = 500;

interface Kept {
  value: number;
  /** The Items of the loop and of the loops around it, when it was kept. */
  items: string;
}

export class Iterations {
  private readonly changed = signal(0);
  /** Changes when a pick or a count changes. */
  readonly version = this.changed.asReadonly();
  private readonly chosen = new Map<string, Kept>();
  /** Counts by the loop's instance key, from the last Execute and from the current data. */
  private executed = new Map<string, Kept>();
  private readonly evaluated = new Map<string, Kept>();
  /** Changes with every pick, so a count asked before it is dropped. */
  private generation = 0;
  private synced: number | null = null;

  constructor(private readonly host: IterationsHost) {}

  /** The picks of the loops around a step, outermost first; empty outside loops. */
  picks(stepId: string): LoopPick[] {
    this.version();
    this.sync();
    return enclosingLoops(this.host.definition(), stepId)
      .reverse()
      .map((loopId) => ({
        loopId,
        index: this.chosen.get(loopId)?.value ?? 0,
      }));
  }

  /** Picks an item of a loop of this draft; anything else is ignored. */
  pick(loopId: string, index: number): void {
    if (!Number.isSafeInteger(index) || index < 0) return;
    this.sync();
    const items = this.itemsAround(loopId);
    if (items === null) return;
    this.chosen.set(loopId, { value: index, items });
    this.generation++;
    this.changed.update((n) => n + 1);
  }

  /** The picked run of a step inside loops (`send[2][0]`); null outside loops. */
  instanceKey(stepId: string): InstanceKey | null {
    const picks = this.picks(stepId);
    if (!picks.length) return null;
    const key = stepId + picks.map(({ index }) => `[${index}]`).join("");
    return isInstanceKey(key) ? key : null;
  }

  /**
   * A step renamed in the editor: a renamed loop keeps its pick and its
   * counts, and so does a loop whose Items read the renamed step, since the
   * rename rewrites that reference and the list stays the same.
   */
  rename(from: string, to: string): void {
    const renamed = new Map([[from, to]]);
    // Rebuilt in order, so the oldest counts still go first.
    const move = (map: Map<string, Kept>) => {
      const entries = [...map].map(([key, kept]): [string, Kept] => [
        renameInstance(key, from, to),
        {
          ...kept,
          items: canonicalJson(
            rewriteStepRefs(JSON.parse(kept.items), renamed),
          ),
        },
      ]);
      map.clear();
      for (const [key, kept] of entries) map.set(key, kept);
    };
    move(this.chosen);
    move(this.executed);
    move(this.evaluated);
    this.synced = null;
    this.changed.update((n) => n + 1);
  }

  /** What the last Execute of this draft counted, for each loop it ran. */
  recordProgress(loops: readonly LoopProgress[]): void {
    const executed = new Map<string, Kept>();
    for (const loop of loops) {
      const key = loop.instanceKey || loop.nodeId;
      if (!isInstanceKey(key) || instanceStep(key) !== loop.nodeId) continue;
      const items = this.itemsAround(loop.nodeId);
      if (items !== null) executed.set(key, { value: loop.count, items });
    }
    this.executed = executed;
    this.changed.update((n) => n + 1);
  }

  /** How many items the loop has for the picks around it. */
  counts(loopId: string): LoopCount {
    this.version();
    this.sync();
    const items = this.itemsAround(loopId);
    if (items === null) return UNKNOWN;
    const key = this.loopKey(loopId);
    for (const kept of [this.executed.get(key), this.evaluated.get(key)])
      if (kept?.items === items) return { count: kept.value, known: true };
    return UNKNOWN;
  }

  /**
   * Counts the loop's items from the current data. An answer that arrives
   * after the draft changed or another item was picked is dropped.
   */
  async evaluate(loopId: string): Promise<LoopCount> {
    this.sync();
    const items = this.itemsAround(loopId);
    if (items === null) return UNKNOWN;
    const revision = this.host.revision();
    const generation = this.generation;
    const key = this.loopKey(loopId);
    let value: unknown = null;
    try {
      value = await this.host.items(loopId, this.picks(loopId));
    } catch {
      value = null;
    }
    if (this.host.revision() !== revision || this.generation !== generation)
      return this.counts(loopId);
    this.evaluated.delete(key);
    if (Array.isArray(value)) {
      if (this.evaluated.size >= MAX_EVALUATED)
        this.evaluated.delete(this.evaluated.keys().next().value!);
      this.evaluated.set(key, { value: value.length, items });
    }
    this.changed.update((n) => n + 1);
    return this.counts(loopId);
  }

  /** The loop's own instance key under the picks around it (`lines[2]`). */
  private loopKey(loopId: string): string {
    return (
      loopId +
      this.picks(loopId)
        .map(({ index }) => `[${index}]`)
        .join("")
    );
  }

  /** The Items of the loop and of the loops around it, outermost first; null when it isn't a loop here. */
  private itemsAround(loopId: string): string | null {
    const definition = this.host.definition();
    const chain = [...enclosingLoops(definition, loopId).reverse(), loopId];
    const items: unknown[] = [];
    for (const id of chain) {
      const pointer = stepPointerOf(definition, id);
      const step =
        pointer === null ? null : getAt(definition, parsePointer(pointer)!);
      if (!isJsonObject(step) || step["kind"] !== "forEach") return null;
      items.push(step["items"] ?? null);
    }
    return canonicalJson(items);
  }

  /** Forgets picks whose loop is gone or whose Items, or those of a loop around it, changed. */
  private sync(): void {
    const revision = this.host.revision();
    if (this.synced === revision) return;
    for (const [loopId, kept] of [...this.chosen])
      if (this.itemsAround(loopId) !== kept.items) this.chosen.delete(loopId);
    this.synced = revision;
  }
}
