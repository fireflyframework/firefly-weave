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
// The Add a step panel's state as data: the views opened so far (a stack),
// the search text and the active option. Keys and clicks reduce it; the
// component draws what `shown` returns. Pure.
import type { Catalog, PanelEntry, PanelSection, PanelView } from "./entries";
import { searchPanel } from "./search";

export interface PanelState {
  readonly stack: readonly string[];
  readonly query: string;
  /** Index into `Shown.options`. */
  readonly active: number;
}
export interface Shown {
  view: PanelView;
  /** The view ← returns to, or null at the start. */
  parent: PanelView | null;
  sections: PanelSection[];
  /** Where each section's options start in `options`. */
  starts: number[];
  options: PanelEntry[];
  note: string;
  searching: boolean;
}
export type PanelAction =
  | { type: "move"; delta: 1 | -1 }
  | { type: "open" }
  | { type: "back" }
  | { type: "enter" }
  | { type: "type"; query: string }
  | { type: "hover"; index: number };
export type PanelEffect =
  | { kind: "none" }
  | { kind: "choose"; entry: PanelEntry };
export interface Reduced {
  state: PanelState;
  effect: PanelEffect;
  /** False when the key belongs to the search field instead. */
  handled: boolean;
}

const NONE: PanelEffect = { kind: "none" };

export const startState = (view: string): PanelState => ({
  stack: [view],
  query: "",
  active: 0,
});

/** The open view, or search results in it (the start views search everything). */
export function shown(
  state: PanelState,
  catalog: Catalog,
  triggersFirst: boolean,
): Shown {
  const view =
    catalog.views.get(state.stack.at(-1) ?? "") ?? catalog.views.get("next")!;
  const parent =
    state.stack.length > 1
      ? (catalog.views.get(state.stack.at(-2)!) ?? null)
      : null;
  const searching = !!state.query.trim();
  let sections = view.sections;
  let note = view.note;
  if (searching) {
    const scope =
      state.stack.length > 1
        ? view.sections.flatMap((section) => section.entries)
        : null;
    const outcome = searchPanel(
      state.query,
      scope,
      catalog.searchable,
      triggersFirst,
      catalog.customCall,
    );
    sections = outcome.sections;
    note = outcome.note;
  }
  const starts: number[] = [];
  let count = 0;
  for (const section of sections) {
    starts.push(count);
    count += section.entries.length;
  }
  return {
    view,
    parent,
    sections,
    starts,
    options: sections.flatMap((section) => section.entries),
    note,
    searching,
  };
}

export function reduce(
  state: PanelState,
  action: PanelAction,
  catalog: Catalog,
  triggersFirst: boolean,
): Reduced {
  const now = shown(state, catalog, triggersFirst);
  const count = now.options.length;
  const active = now.options[state.active];
  switch (action.type) {
    case "move":
      return {
        state: {
          ...state,
          active: count ? (state.active + action.delta + count) % count : 0,
        },
        effect: NONE,
        handled: true,
      };
    case "hover":
      return {
        state: {
          ...state,
          active: Math.max(0, Math.min(action.index, count - 1)),
        },
        effect: NONE,
        handled: true,
      };
    case "type":
      return {
        state: { ...state, query: action.query, active: 0 },
        effect: NONE,
        handled: true,
      };
    case "open":
      if (!active?.open || active.disabled)
        return { state, effect: NONE, handled: false };
      return {
        state: { stack: [...state.stack, active.open], query: "", active: 0 },
        effect: NONE,
        handled: true,
      };
    case "back": {
      if (state.stack.length < 2)
        return { state, effect: NONE, handled: false };
      const from = state.stack.at(-1)!;
      const previous: PanelState = {
        stack: state.stack.slice(0, -1),
        query: "",
        active: 0,
      };
      const index = shown(previous, catalog, triggersFirst).options.findIndex(
        (option) => option.open === from,
      );
      return {
        state: { ...previous, active: Math.max(0, index) },
        effect: NONE,
        handled: true,
      };
    }
    default: {
      if (!active || active.disabled)
        return { state, effect: NONE, handled: true };
      if (active.open)
        return reduce(state, { type: "open" }, catalog, triggersFirst);
      return {
        state,
        effect: { kind: "choose", entry: active },
        handled: true,
      };
    }
  }
}
