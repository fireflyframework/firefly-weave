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
// Loop bodies: which loops are around a step (their items are `item`,
// `index` and `loops.<id>` in a formula there), and what a field in a loop
// body can read, which the Studio host computes with the compiler
// (POST /studio/local/scope) because Studio's offline scope doesn't model
// loops. Answers are kept per document and field.
import type { HostEntry } from "./complete";

export interface HostScopeEntry extends HostEntry {
  source: string;
  loop?: string;
  path: string[];
  label: string;
  breadcrumb: string;
  types: string[];
  optional: boolean;
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

/** Every nested step list of a step, with its pointer and whether it is a loop body. */
function blocks(
  step: Record<string, unknown>,
  at: string,
): { steps: unknown[]; at: string; loop: boolean }[] {
  const found: { steps: unknown[]; at: string; loop: boolean }[] = [];
  const add = (block: unknown, path: string, loop = false) => {
    if (isRecord(block) && Array.isArray(block["steps"]))
      found.push({ steps: block["steps"], at: `${at}${path}/steps`, loop });
  };
  if (Array.isArray(step["cases"]))
    step["cases"].forEach((block, i) => add(block, `/cases/${i}`));
  add(step["default"], "/default");
  if (isRecord(step["branches"]))
    for (const [name, block] of Object.entries(step["branches"]))
      add(block, `/branches/${name.replace(/~/g, "~0").replace(/\//g, "~1")}`);
  add(step["body"], "/body", step["kind"] === "forEach");
  add(step["errorPath"], "/errorPath");
  return found;
}

function find(
  steps: unknown,
  at: string,
  loops: string[],
  stepId: string,
): { pointer: string; loops: string[] } | null {
  if (!Array.isArray(steps)) return null;
  for (let i = 0; i < steps.length; i++) {
    const step = steps[i];
    if (!isRecord(step)) continue;
    const pointer = `${at}/${i}`;
    if (step["id"] === stepId) return { pointer, loops };
    for (const block of blocks(step, pointer)) {
      const inner = find(
        block.steps,
        block.at,
        block.loop ? [String(step["id"]), ...loops] : loops,
        stepId,
      );
      if (inner) return inner;
    }
  }
  return null;
}

const locate = (definition: unknown, stepId: string) =>
  isRecord(definition) && isRecord(definition["spec"])
    ? find(definition["spec"]["steps"], "/spec/steps", [], stepId)
    : null;

/** The loops around a step, innermost first; empty outside loops. */
export function enclosingLoops(definition: unknown, stepId: string): string[] {
  return locate(definition, stepId)?.loops ?? [];
}

/** Where a step sits in the workflow document, as a JSON Pointer. */
export function stepPointerOf(
  definition: unknown,
  stepId: string,
): string | null {
  return locate(definition, stepId)?.pointer ?? null;
}

export class LoopScopes {
  private readonly answers = new Map<string, HostScopeEntry[] | null>();
  constructor(
    private readonly request: (body: {
      source: string;
      format: "json";
      path: string;
    }) => Promise<unknown>,
    private readonly changed: () => void,
  ) {}

  /** What the field at `pointer` can read; null while the host is asked. */
  entries(definition: unknown, pointer: string): HostScopeEntry[] | null {
    const source = JSON.stringify(definition);
    const key = `${pointer}\n${source}`;
    if (this.answers.has(key)) return this.answers.get(key) ?? null;
    if (this.answers.size > 64) this.answers.clear();
    this.answers.set(key, null);
    void this.request({ source, format: "json", path: pointer }).then(
      (answer) => {
        const entries =
          isRecord(answer) && Array.isArray(answer["entries"])
            ? answer["entries"]
            : [];
        this.answers.set(
          key,
          entries.filter(isRecord) as unknown as HostScopeEntry[],
        );
        this.changed();
      },
      () => {
        this.answers.set(key, []);
        this.changed();
      },
    );
    return null;
  }
}
