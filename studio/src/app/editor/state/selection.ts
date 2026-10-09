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
// The canvas selection: which steps are selected, which one has focus, and
// whether they form one run of consecutive steps. Copy, cut, duplicate,
// move and extract need such a run; delete and pinning work on any
// selection. A selected group brings every step inside it.

/** Where a step sits: its sequence ("root" or "<group>/<lane>"), index and group. */
export interface StepPlace {
  id: string;
  owner: string;
  index: number;
  parent: string | null;
  /** Position in document order. */
  order: number;
}
export interface Selection {
  readonly ids: readonly string[];
  /** The step with focus: the one step details show. */
  readonly focus: string | null;
}
export const NO_SELECTION: Selection = { ids: [], focus: null };

export const only = (id: string): Selection => ({ ids: [id], focus: id });

export function selectionOf(
  ids: readonly string[],
  focus: string | null = ids[0] ?? null,
): Selection {
  const unique = [...new Set(ids)];
  return {
    ids: unique,
    focus:
      focus !== null && unique.includes(focus) ? focus : (unique[0] ?? null),
  };
}

/** Shift or Ctrl/Cmd+click: adds a step (and focuses it) or removes it. */
export function toggled(selection: Selection, id: string): Selection {
  if (!selection.ids.includes(id))
    return { ids: [...selection.ids, id], focus: id };
  const ids = selection.ids.filter((item) => item !== id);
  return {
    ids,
    focus:
      selection.focus === id ? (ids[ids.length - 1] ?? null) : selection.focus,
  };
}

/** True when a group above the step is selected. */
function inside(
  places: ReadonlyMap<string, StepPlace>,
  id: string,
  chosen: ReadonlySet<string>,
): boolean {
  for (
    let parent = places.get(id)?.parent ?? null;
    parent;
    parent = places.get(parent)?.parent ?? null
  )
    if (chosen.has(parent)) return true;
  return false;
}

/** The selected steps no selected group holds, in document order. */
export function topLevel(
  selection: Selection,
  places: ReadonlyMap<string, StepPlace>,
): string[] {
  const chosen = new Set(selection.ids.filter((id) => places.has(id)));
  return [...chosen]
    .filter((id) => !inside(places, id, chosen))
    .sort((a, b) => places.get(a)!.order - places.get(b)!.order);
}

/**
 * Shift or Ctrl/Cmd+click on the canvas, where a selected group brings
 * every step inside it. A step the selection doesn't cover comes in (a
 * group in place of the selected steps inside it); a covered step goes
 * out, and the selected groups around it give way to the steps inside
 * them that stay selected. The result never lists a group together with
 * a step inside it.
 */
export function toggledCovering(
  selection: Selection,
  id: string,
  places: ReadonlyMap<string, StepPlace>,
): Selection {
  const chosen = covered(selection, places);
  const clicked = new Set([id]);
  if (!chosen.has(id))
    return {
      ids: [
        ...selection.ids.filter((item) => !inside(places, item, clicked)),
        id,
      ],
      focus: id,
    };
  if (!inside(places, id, new Set(selection.ids)))
    return toggled(selection, id);
  const kept = new Set(
    [...chosen].filter(
      (item) =>
        item !== id &&
        !inside(places, item, clicked) &&
        !inside(places, id, new Set([item])),
    ),
  );
  const ids = [...kept]
    .filter((item) => !kept.has(places.get(item)?.parent ?? ""))
    .sort((a, b) => places.get(a)!.order - places.get(b)!.order);
  return {
    ids,
    focus:
      selection.focus !== null && ids.includes(selection.focus)
        ? selection.focus
        : (ids[ids.length - 1] ?? null),
  };
}

/** Every step the selection covers: the selected ones and everything inside selected groups. */
export function covered(
  selection: Selection,
  places: ReadonlyMap<string, StepPlace>,
): Set<string> {
  const chosen = new Set(selection.ids.filter((id) => places.has(id)));
  return new Set(
    [...places.keys()].filter(
      (id) => chosen.has(id) || inside(places, id, chosen),
    ),
  );
}

export type Contiguity =
  | { ok: true; owner: string; from: number; to: number; ids: string[] }
  | { ok: false; reason: "empty" | "spread" };

/** One run: the top-level steps are consecutive steps of one sequence. */
export function contiguity(
  selection: Selection,
  places: ReadonlyMap<string, StepPlace>,
): Contiguity {
  const ids = topLevel(selection, places);
  if (!ids.length) return { ok: false, reason: "empty" };
  const list = ids
    .map((id) => places.get(id)!)
    .sort((a, b) => a.index - b.index);
  const owner = list[0].owner;
  if (
    list.some(
      (item, i) => item.owner !== owner || item.index !== list[0].index + i,
    )
  )
    return { ok: false, reason: "spread" };
  return {
    ok: true,
    owner,
    from: list[0].index,
    to: list[list.length - 1].index,
    ids: list.map((item) => item.id),
  };
}

export type RunCommand = "copy" | "cut" | "duplicate" | "move" | "extract";
const VERBS: Record<RunCommand, string> = {
  copy: "Copy",
  cut: "Cut",
  duplicate: "Duplicate",
  move: "Move",
  extract: "Extract to sub-workflow",
};
/** Why a command that needs one run can't run on this selection; null when it can. */
export function blockedReason(
  command: RunCommand,
  selection: Selection,
  places: ReadonlyMap<string, StepPlace>,
): string | null {
  const run = contiguity(selection, places);
  if (run.ok) return null;
  if (run.reason === "empty") return "Select a step first.";
  return `${VERBS[command]} works on steps next to each other in one path. Select a single run of steps.`;
}

/** Shift+← / Shift+→: adds the step before or after the focused one in its sequence. */
export function extended(
  selection: Selection,
  places: ReadonlyMap<string, StepPlace>,
  direction: "upstream" | "downstream",
): Selection {
  const focus = selection.focus ? places.get(selection.focus) : undefined;
  if (!focus) return selection;
  const index = focus.index + (direction === "upstream" ? -1 : 1);
  const next = [...places.values()].find(
    (item) => item.owner === focus.owner && item.index === index,
  );
  if (!next) return selection;
  return {
    ids: selection.ids.includes(next.id)
      ? selection.ids
      : [...selection.ids, next.id],
    focus: next.id,
  };
}

/** Whether moving a step there changes anything, and keeps a group out of its own lanes. */
export function moveAllowed(
  places: ReadonlyMap<string, StepPlace>,
  id: string,
  insert: { owner: string; index: number },
): boolean {
  const place = places.get(id);
  if (!place) return false;
  if (
    insert.owner === place.owner &&
    (insert.index === place.index || insert.index === place.index + 1)
  )
    return false;
  for (let owner = insert.owner; owner !== "root"; ) {
    const group = owner.slice(0, owner.indexOf("/"));
    if (group === id) return false;
    owner = places.get(group)?.owner ?? "root";
  }
  return true;
}
