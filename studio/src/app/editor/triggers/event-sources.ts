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
// The workflows a platform event trigger can be told to react to: the
// project's published ones, then the drafts on this computer, which may be
// published under their name later. Only names count: a trigger reacts to
// every version of a workflow.
import { compareVersions, type LocalCallee } from "../calls/callable-catalog";
import type { CatalogLockState } from "../calls/catalog-lock";
import { WORKFLOW_NAME } from "./event-trigger";

export interface SourceSuggestion {
  name: string;
  /** The latest published version, or that it isn't published. */
  aside: string;
}
export interface SourceGroup {
  label: string;
  items: SourceSuggestion[];
}

/** Why the project's workflows are missing, or null when they're listed or there is no project. */
function missingNote(lock: CatalogLockState): string | null {
  switch (lock.kind) {
    case "forbidden":
      return "You can't read this project's published workflows here.";
    case "failed":
      return "Studio couldn't read this project's published workflows.";
    case "loading":
      return "Reading this project's published workflows…";
    case "offline":
    case "ready":
      return null;
  }
}

/**
 * The workflows whose name holds the text typed, by name, without the ones
 * already chosen and the workflow itself.
 */
export function sourceSuggestions(request: {
  query: string;
  chosen: readonly string[];
  /** The workflow that reacts. */
  own: string;
  lock: CatalogLockState;
  drafts: readonly LocalCallee[];
}): { note: string | null; groups: SourceGroup[] } {
  const query = request.query.trim().toLowerCase();
  const offered = (name: string) =>
    name !== request.own &&
    !request.chosen.includes(name) &&
    name.toLowerCase().includes(query);
  const latest = new Map<string, string>();
  if (request.lock.kind === "ready")
    for (const { name, version } of request.lock.lock.workflows.values()) {
      const known = latest.get(name);
      if (known === undefined || compareVersions(version, known) > 0)
        latest.set(name, version);
    }
  const byName = (a: SourceSuggestion, b: SourceSuggestion) =>
    a.name < b.name ? -1 : 1;
  const drafts = new Set(
    request.drafts
      .map((draft) => draft.name)
      .filter((name) => WORKFLOW_NAME.test(name) && !latest.has(name)),
  );
  const groups: SourceGroup[] = [
    {
      label: "In this project",
      items: [...latest]
        .filter(([name]) => offered(name))
        .map(([name, version]) => ({ name, aside: version }))
        .sort(byName),
    },
    {
      label: "On this computer",
      items: [...drafts]
        .filter(offered)
        .map((name) => ({ name, aside: "Draft, not published" }))
        .sort(byName),
    },
  ];
  return {
    note: missingNote(request.lock),
    groups: groups.filter((group) => group.items.length),
  };
}
