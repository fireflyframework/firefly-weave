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
// Fingerprints that tell when pinned output may be out of date: a 64-bit
// FNV-1a over canonical JSON of what a step is (its own settings, without
// the steps it holds), what it calls (the action, table, workflow or AI
// profile behind it) and what it reads (the steps it references, each with
// its own fingerprint, and the test input when it reads the workflow
// input). A step inside a loop that reads the loop's item, index or
// `/loops/<id>` also reads what that loop goes over: its items expression,
// the steps that expression reads and the test input when it reads it. They
// detect changes; they protect nothing.
import { canonicalJson } from "../../forms/core/json";
import type { Step, Workflow } from "../../model";
import { childLists, withChildIds } from "../ndv/step-walk";
import type { Json, StaleReason } from "../ndv/registry";
import type { Fingerprint, PinEntry } from "./test-data";

/** FNV-1a 64 of the UTF-8 bytes of `text`, as 16 lowercase hex digits (four 16-bit limbs, no BigInt). */
export function fnv1a64(text: string): string {
  let h0 = 0x2325;
  let h1 = 0x8422;
  let h2 = 0x9ce4;
  let h3 = 0xcbf2;
  for (const byte of new TextEncoder().encode(text)) {
    h0 ^= byte;
    // h * 0x100000001b3 = h * 0x1b3 + (h << 40), modulo 2^64.
    const t0 = h0 * 0x1b3;
    let t1 = h1 * 0x1b3;
    let t2 = h2 * 0x1b3 + (h0 << 8);
    const t3 = h3 * 0x1b3 + (h1 << 8);
    t1 += t0 >>> 16;
    h0 = t0 & 0xffff;
    t2 += t1 >>> 16;
    h1 = t1 & 0xffff;
    h3 = (t3 + (t2 >>> 16)) & 0xffff;
    h2 = t2 & 0xffff;
  }
  return [h3, h2, h1, h0]
    .map((limb) => limb.toString(16).padStart(4, "0"))
    .join("");
}
/** `fnv1a64:` and the hash of the value's canonical JSON (sorted keys, no whitespace). */
export function fingerprint(value: unknown): Fingerprint {
  return `fnv1a64:${fnv1a64(canonicalJson(value ?? null))}`;
}

export interface StepFingerprints {
  config: Fingerprint;
  contract: Fingerprint;
  upstream: Fingerprint;
  /** All three together: what a later step that reads this one depends on. */
  execution: Fingerprint;
}
export interface FingerprintInputs {
  workflow: Workflow;
  /** The test input steps that read the workflow input depend on. */
  testInput: Json | undefined;
  /**
   * What the step calls, as text: its `uses` for a published version, a
   * fingerprint of the document for one that only exists here (an action
   * this workflow owns, an AI profile), "" when it calls nothing.
   */
  contractOf(step: Step): string;
}

const unescape = (segment: string) =>
  segment.replace(/~1/g, "/").replace(/~0/g, "~");
const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

/** A value with the steps and loops it names replaced by their order of first appearance. */
interface Named {
  value: unknown;
  /** The steps it reads or holds, in label order. */
  steps: string[];
  /** The loops it reads (`/item`, `/index`, `/loops/<id>`), in label order. */
  loops: string[];
  readsInput: boolean;
}
/**
 * Names are not content: a step's fingerprint replaces every step and loop
 * ID it mentions (in references and in the lists of steps it holds) with its
 * order of first appearance in canonical key order, and its own ID with
 * `$self`. Renaming a step, which rewrites its readers' references, then
 * changes nothing; reading another step changes what it reads (`upstream`).
 */
function named(
  root: unknown,
  self: string,
  enclosing: readonly string[],
): Named {
  const steps: string[] = [];
  const loops: string[] = [];
  let readsInput = false;
  const label = (list: string[], id: string, prefix: string) => {
    let index = list.indexOf(id);
    if (index < 0) index = list.push(id) - 1;
    return `${prefix}${index}`;
  };
  const pointer = (text: string): string => {
    if (text === "/input" || text.startsWith("/input/")) readsInput = true;
    if (/^\/(?:item|index)(?:\/|$)/.test(text) && enclosing.length > 0)
      label(loops, enclosing[enclosing.length - 1]!, "$loop");
    const match = /^\/(steps|loops)\/([^/]+)(.*)$/s.exec(text);
    if (!match) return text;
    const [, root_, segment, rest] = match as unknown as [
      string,
      string,
      string,
      string,
    ];
    const id = unescape(segment);
    if (root_ === "loops") return `/loops/${label(loops, id, "$loop")}${rest}`;
    return id === self
      ? `/steps/$self${rest}`
      : `/steps/${label(steps, id, "$")}${rest}`;
  };
  const sorted = (node: Record<string, unknown>, children: boolean) =>
    Object.fromEntries(
      Object.keys(node)
        .sort()
        .map((key) => {
          const value = node[key];
          // The lists of steps a group holds, as withChildIds leaves them: IDs.
          if (
            children &&
            key === "steps" &&
            Array.isArray(value) &&
            value.every((item) => typeof item === "string")
          )
            return [key, value.map((id) => label(steps, id, "$"))];
          return [key, map(value)];
        }),
    );
  const map = (node: unknown): unknown => {
    if (Array.isArray(node)) return node.map(map);
    if (!isRecord(node)) return node;
    const keys = Object.keys(node);
    if (keys.length === 1 && typeof node["ref"] === "string")
      return { ref: pointer(node["ref"]) };
    if (keys.length === 1 && "literal" in node) return node;
    if (keys.length === 1 && isRecord(node["object"]))
      return { object: sorted(node["object"], false) };
    return sorted(node, true);
  };
  return { value: map(root), steps, loops, readsInput };
}

/** Every step's fingerprints, by step ID, in one pass (each step is hashed once). */
export function fingerprints(
  inputs: FingerprintInputs,
): Map<string, StepFingerprints> {
  const steps = new Map<string, Step>();
  // The loops around each step, outermost first.
  const loopsAround = new Map<string, readonly string[]>();
  const visit = (list: readonly Step[], enclosing: readonly string[]) => {
    for (const step of list) {
      if (steps.has(step.id)) continue;
      steps.set(step.id, step);
      loopsAround.set(step.id, enclosing);
      const inside =
        (step.kind as string) === "forEach"
          ? [...enclosing, step.id]
          : enclosing;
      for (const inner of childLists(step)) visit(inner, inside);
    }
  };
  visit(inputs.workflow.spec.steps, []);
  const input = fingerprint(inputs.testInput ?? null);
  // What one loop goes over: its items expression and everything that expression reads.
  const iterations = new Map<string, Fingerprint | null>();
  const iterating = new Set<string>();
  const iteration = (id: string): Fingerprint | null => {
    if (iterations.has(id)) return iterations.get(id)!;
    const loop = steps.get(id);
    if (!loop || (loop.kind as string) !== "forEach" || iterating.has(id))
      return null;
    iterating.add(id);
    const items = named(loop["items"] ?? null, id, loopsAround.get(id) ?? []);
    const value = fingerprint([
      items.value,
      items.steps.map((dep) => of(dep)?.execution ?? null),
      items.loops.map(iteration),
      items.readsInput ? input : null,
    ]);
    iterating.delete(id);
    iterations.set(id, value);
    return value;
  };
  const done = new Map<string, StepFingerprints>();
  const busy = new Set<string>();
  // Hoisted: iteration() and of() call each other.
  function of(id: string): StepFingerprints | null {
    const cached = done.get(id);
    if (cached) return cached;
    const step = steps.get(id);
    // A missing step or a reference cycle (a workflow that doesn't compile) counts as nothing.
    if (!step || busy.has(id)) return null;
    busy.add(id);
    const own: Record<string, unknown> = { ...withChildIds(step) };
    delete own["id"];
    const self = named(own, id, loopsAround.get(id) ?? []);
    const upstream = fingerprint([
      self.steps.map((dep) => of(dep)?.execution ?? null),
      self.readsInput ? input : null,
      self.loops.map(iteration),
    ]);
    const config = fingerprint(self.value);
    const contract = fingerprint(inputs.contractOf(step));
    const result: StepFingerprints = {
      config,
      contract,
      upstream,
      execution: fingerprint([config, contract, upstream]),
    };
    busy.delete(id);
    done.set(id, result);
    return result;
  }
  for (const id of steps.keys()) of(id);
  return done;
}

/** Why pinned output may be out of date, first reason first, or null. */
export function staleReason(
  pin: Pick<PinEntry, "configHash" | "contractHash" | "upstreamHash">,
  now: StepFingerprints | undefined,
): StaleReason | null {
  if (!now || pin.configHash !== now.config) return "settings";
  if (pin.contractHash !== now.contract) return "contract";
  if (pin.upstreamHash !== now.upstream) return "upstream";
  return null;
}
/** The words for a stale reason, after "Pinned test data, out of date: ". */
export const STALE_WORDS: Record<StaleReason, string> = {
  settings: "this step's settings changed",
  contract: "what this step calls changed",
  upstream: "what this step reads changed",
};
