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
// The workflows a definition calls, and the activation of each that a new
// activation pins (`workflow_activation_ids`). Calls are found the way the
// platform's `called_references` finds them: call steps, the workflows a step
// in a transaction calls to confirm, cancel or reverse it, and the workflows
// an AI agent may call as tools, block by block. A callee's current
// activation is the latest activation revision of its name, as the CLI's
// --pin-current-calls picks it: it must run the called version and must not
// be retired.
import { escapeSegment, isJsonObject } from "../../forms/core/json";
import type { Step } from "../../model";
import { blocksOf } from "../step-blocks";

/** A called workflow version and where the definition calls it. */
export interface CalledReference {
  /** `name@version`. */
  reference: string;
  /** The JSON pointer of the reference, as the compiler's diagnostics point to it. */
  path: string;
}
/** A published workflow version. */
export interface CalleeVersion {
  id: string;
  name: string;
  version: string;
}
/** An activation in the environment. */
export interface CalleeActivation {
  id: string;
  /** The workflow's name. */
  name: string;
  revision: number;
  retired: boolean;
  versionId: string;
  createdAt?: string;
}
/** A compiler diagnostic, as Studio receives it. */
export interface CallDiagnostic {
  code?: string;
  message: string;
  path?: unknown;
}

export type CalleeRowKind = "proposed" | "missing" | "not-callable";
export interface CalleeRow {
  kind: CalleeRowKind;
  reference: string;
  /** The activation the new activation pins; proposed rows only. */
  activationId?: string;
  text: string;
}

const NOT_CALLABLE = "WV-COMP-CALL_NOT_CALLABLE";
const UNDO = ["confirm", "cancel", "compensate"] as const;

/** Every called reference with its location, in the order the platform lists them. */
export function calledReferenceSites(definition: unknown): CalledReference[] {
  const found: CalledReference[] = [];
  const spec = isJsonObject(definition) ? definition["spec"] : undefined;
  if (!isJsonObject(spec)) return found;
  const blocks: [unknown, string][] = [[spec["steps"], "/spec/steps"]];
  // Breadth first, like the platform: a block's steps before the blocks inside them.
  for (let next = 0; next < blocks.length; next++) {
    const [steps, path] = blocks[next];
    if (!Array.isArray(steps)) continue;
    steps.forEach((step: unknown, index) => {
      if (!isJsonObject(step)) return;
      const location = `${path}/${index}`;
      const kind = step["kind"];
      if (kind === "callWorkflow" && typeof step["uses"] === "string")
        found.push({ reference: step["uses"], path: `${location}/uses` });
      if (kind === "agent" && Array.isArray(step["tools"]))
        step["tools"].forEach((tool, number) => {
          if (isJsonObject(tool) && typeof tool["workflow"] === "string")
            found.push({
              reference: tool["workflow"],
              path: `${location}/tools/${number}/workflow`,
            });
        });
      for (const name of UNDO) {
        const undo = step[name];
        if (
          isJsonObject(undo) &&
          undo["kind"] === "callWorkflow" &&
          typeof undo["uses"] === "string"
        )
          found.push({
            reference: undo["uses"],
            path: `${location}/${name}/uses`,
          });
      }
      // The platform lists a parallel step's branches by name.
      const own = blocksOf(step as unknown as Step);
      const byName = own
        .filter((block) => block.kind === "branch")
        .sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
      for (const block of own.map((block) =>
        block.kind === "branch" ? byName.shift()! : block,
      )) {
        const branch: unknown = block.branch;
        if (!isJsonObject(branch)) continue;
        const path = block.path
          .map((segment) => `/${escapeSegment(String(segment))}`)
          .join("");
        blocks.push([branch["steps"], `${location}${path}/steps`]);
      }
    });
  }
  return found;
}

/** The exact workflow references a definition calls, in the platform's order. */
export const calledReferences = (definition: unknown): string[] =>
  calledReferenceSites(definition).map((site) => site.reference);

const shortDate = (value: string) => {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? undefined
    : new Intl.DateTimeFormat("en-GB", {
        day: "numeric",
        month: "short",
      }).format(date);
};

/**
 * One row per called version: the activation to pin, or why there is none.
 * `diagnostics` are the compiler's findings for this definition; a callee
 * that doesn't allow this caller shows the compiler's message.
 */
export function proposePins(
  references: CalledReference[],
  versions: CalleeVersion[],
  activations: CalleeActivation[],
  diagnostics: CallDiagnostic[] = [],
): CalleeRow[] {
  const versionIds = new Map(
    versions.map((item) => [`${item.name}@${item.version}`, item.id]),
  );
  const latest = new Map<string, CalleeActivation>();
  for (const activation of activations) {
    const known = latest.get(activation.name);
    if (!known || activation.revision > known.revision)
      latest.set(activation.name, activation);
  }
  const refusals = new Map<string, string>();
  for (const diagnostic of diagnostics)
    if (diagnostic.code === NOT_CALLABLE && typeof diagnostic.path === "string")
      refusals.set(diagnostic.path, diagnostic.message);

  const rows: CalleeRow[] = [];
  const seen = new Set<string>();
  for (const { reference } of references) {
    if (seen.has(reference)) continue;
    seen.add(reference);
    const refusal = references.find(
      (site) => site.reference === reference && refusals.has(site.path),
    );
    if (refusal) {
      rows.push({
        kind: "not-callable",
        reference,
        text: refusals.get(refusal.path)!,
      });
      continue;
    }
    const [name, version] = reference.split("@");
    const current = latest.get(name);
    if (
      !current ||
      current.retired ||
      current.versionId !== versionIds.get(reference)
    ) {
      rows.push({ kind: "missing", reference, text: `Activate ${name} first` });
      continue;
    }
    const made = current.createdAt ? shortDate(current.createdAt) : undefined;
    rows.push({
      kind: "proposed",
      reference,
      activationId: current.id,
      text: `${name} ${version} · ${made ? `activation of ${made}` : `activation ${current.revision}`}`,
    });
  }
  return rows;
}

/** The `workflow_activation_ids` the proposed rows pin. */
export function callPins(rows: CalleeRow[]): Record<string, string> {
  const pins: Record<string, string> = {};
  for (const row of rows)
    if (row.kind === "proposed" && row.activationId)
      pins[row.reference] = row.activationId;
  return pins;
}
