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
// A platform check of a workflow whose steps use decision tables or actions
// kept as drafts on this computer: the platform doesn't know those versions
// yet. Each such "unknown" reference becomes the error "Publish X before
// publishing this workflow." at its step, so Publish… stops there and says
// what to publish. Drafts are never published as a side effect.
import { getAt, parsePointer } from "../../../forms/core/json";
import type { CanvasSidecar } from "../../state/canvas-sidecar";
import { accountTables, type AccountHost } from "../../state/host-definitions";
import { withOwnedPending, type PendingAware } from "./owned-pending";
import { ownedActionsOf } from "./owned-store";

export const DRAFT_UNPUBLISHED = "WV-STUDIO-DRAFT_UNPUBLISHED";
const UNKNOWN = new Set(["WV-COMP-UNKNOWN_RESOURCE", "WV-COMP-UNKNOWN_ACTION"]);

export const publishFirst = (uses: string) =>
  `Publish ${uses} before publishing this workflow.`;

/** Unknown references to drafts on this computer, as errors that name the draft to publish. */
export function withDraftPending<T extends PendingAware>(
  result: T,
  definition: unknown,
  drafts: ReadonlySet<string>,
): T & { draftsPending?: number } {
  if (!drafts.size || !Array.isArray(result.diagnostics)) return result;
  let pending = 0;
  const diagnostics = result.diagnostics.map((diagnostic) => {
    if (!UNKNOWN.has(String(diagnostic.code)) || diagnostic.severity === "info")
      return diagnostic;
    const at = parsePointer(String(diagnostic["path"] ?? ""));
    const uses = at ? getAt(definition, at) : undefined;
    if (typeof uses !== "string" || !drafts.has(uses)) return diagnostic;
    pending++;
    return {
      ...diagnostic,
      code: DRAFT_UNPUBLISHED,
      severity: "error",
      message: publishFirst(uses),
    };
  });
  return pending ? { ...result, diagnostics, draftsPending: pending } : result;
}

export type DraftCheckHost = AccountHost;

/** The `name@version` of every draft the signed-in account keeps on this computer. */
export async function draftUses(host: DraftCheckHost): Promise<Set<string>> {
  const tables = await accountTables(host);
  return new Set(
    (tables?.entries() ?? [])
      .filter((entry) => entry.kind === "draft")
      .map((entry) => `${entry.name}@${entry.version}`),
  );
}

/** The platform's check as Studio shows it: owned actions publish first, drafts must be published before. */
export async function withPending<T extends PendingAware>(
  result: T,
  definition: unknown,
  canvas: CanvasSidecar,
  host: DraftCheckHost,
): Promise<T> {
  const owned = Object.keys(ownedActionsOf(canvas)).length
    ? withOwnedPending(result, definition, canvas)
    : result;
  return withDraftPending(owned, definition, await draftUses(host));
}
