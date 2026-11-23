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
// The blocks of a step: the step lists it holds, each with its own `steps`
// and `output` (the language's Branch). A decision's paths and Otherwise, a
// parallel step's branches, a loop's body, a transaction's own steps, and the
// error path any step with On error "Continue with error output" holds. Every
// walk of a workflow's structure (sequences, IDs, navigation, copy and paste,
// Execute's slice, scope) reads blocks here, so a new block kind is one entry.
import type { Branch, Step } from "../model";

/** The field, and the block name, of a step's error path: its owner is "<step>/errorPath". */
export const ERROR_PATH = "errorPath";

/**
 * The step whose output a step's error path gives as its result ("Result on
 * error · from flag-request"); null when the result isn't one step's output.
 */
export function errorResultSource(
  step: Step | null | undefined,
): string | null {
  const path = step?.[ERROR_PATH];
  const output =
    path && typeof path === "object" && !Array.isArray(path)
      ? (path as Record<string, unknown>)["output"]
      : undefined;
  const ref =
    output && typeof output === "object" && !Array.isArray(output)
      ? (output as Record<string, unknown>)["ref"]
      : undefined;
  const found =
    typeof ref === "string" ? /^\/steps\/([^/]+)\/output$/.exec(ref) : null;
  return found ? found[1].replace(/~1/g, "/").replace(/~0/g, "~") : null;
}

export type BlockKind = "case" | "default" | "branch" | "body" | "errorPath";
export interface StepBlock {
  /**
   * The block's name in its sequence's owner, "<step>/<name>": "case 1" (from
   * 1), "default", a parallel branch's own name, "body", "steps" (a
   * transaction's own steps) or "errorPath".
   */
  name: string;
  /** Where the block sits, relative to the step; its steps are at path + "steps". [] is the step itself. */
  path: (string | number)[];
  /**
   * The block as the document holds it. Studio's model only holds valid
   * branches; a walk of untrusted text checks `steps` before it reads them.
   */
  branch: Branch;
  kind: BlockKind;
}

// Kept small: the model and the designer read blocks on the first page.
const block = (
  name: string,
  path: (string | number)[],
  branch: unknown,
  kind = name as BlockKind,
): StepBlock => ({ name, path, branch: branch as Branch, kind });

/**
 * A step's own blocks, by kind; a kind without blocks holds none. A
 * decision without a list of paths, or a parallel step without branches,
 * reads as one block that is no branch (0): the model's check refuses it,
 * and walks find nothing in it.
 */
function ownBlocks(step: Step): StepBlock[] {
  const { cases, branches } = step;
  switch (step.kind as string) {
    case "switch":
      return [
        ...(Array.isArray(cases) ? cases : [0]).map((branch, index) =>
          block(`case ${index + 1}`, ["cases", index], branch, "case"),
        ),
        block("default", ["default"], step["default"]),
      ];
    case "parallel":
      // Read as entries whatever object holds them, as the model always has.
      return Object.entries(
        branches && typeof branches === "object" ? branches : { "": 0 },
      ).map(([name, branch]) =>
        block(name, ["branches", name], branch, "branch"),
      );
    case "forEach":
      return [block("body", ["body"], step["body"])];
    case "transaction":
      // A transaction is a block itself: its own steps and output.
      return [block("steps", [], step, "body")];
  }
  return [];
}

/**
 * The blocks of a step in document order: its own blocks, then its error
 * path, as the platform orders them (the Studio host's `_branches`).
 */
export function blocksOf(step: Step): StepBlock[] {
  const blocks = ownBlocks(step);
  if (step[ERROR_PATH] !== undefined)
    blocks.push(block(ERROR_PATH, [ERROR_PATH], step[ERROR_PATH]));
  return blocks;
}
