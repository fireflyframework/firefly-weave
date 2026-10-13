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
// The completion list: an ARIA listbox in a CodeMirror tooltip, driven by
// complete.ts. It opens while typing a name or after `.` (or when asked),
// follows the caret, and closes on Escape, blur, or when nothing matches.
// ↑ and ↓ move, Enter and Tab accept, a click accepts.
import {
  EditorSelection,
  Facet,
  Prec,
  StateEffect,
  StateField,
  type EditorState,
  type Extension,
  type Transaction,
} from "@codemirror/state";
import {
  EditorView,
  keymap,
  showTooltip,
  type Tooltip,
  type TooltipView,
} from "@codemirror/view";
import type { CompletionList } from "../complete";

export interface CompletionConfig {
  source(): ((text: string, caret: number) => CompletionList | null) | null;
  listId(): string;
}
interface Open {
  list: CompletionList;
  active: number;
}

const completionConfig = Facet.define<CompletionConfig, CompletionConfig>({
  combine: (values) => values[0],
});
const setOpen = StateEffect.define<Open | null>();

function compute(state: EditorState): Open | null {
  const source = state.facet(completionConfig).source();
  if (!source) return null;
  const list = source(state.doc.toString(), state.selection.main.head);
  return list && list.options.length ? { list, active: 0 } : null;
}

function opens(transaction: Transaction): boolean {
  if (!transaction.isUserEvent("input.type")) return false;
  let typed = "";
  transaction.changes.iterChanges(
    (_a, _b, _c, _d, inserted) => (typed += inserted.toString()),
  );
  return /[.A-Za-z_(]$/.test(typed);
}

const completionState = StateField.define<Open | null>({
  create: () => null,
  update(value, transaction) {
    for (const effect of transaction.effects)
      if (effect.is(setOpen)) return effect.value;
    if (!transaction.docChanged && !transaction.selection) return value;
    if (!value && !opens(transaction)) return null;
    const next = compute(transaction.state);
    if (!next) return null;
    return value && value.list.from === next.list.from
      ? {
          list: next.list,
          active: Math.min(value.active, next.list.options.length - 1),
        }
      : next;
  },
  provide: (field) => [
    showTooltip.computeN([field], (state) => {
      const open = state.field(field);
      return open ? [tooltip(open.list.from)] : [];
    }),
    EditorView.contentAttributes.compute(
      [field, completionConfig],
      (state): Record<string, string> => {
        const open = state.field(field);
        const id = state.facet(completionConfig).listId();
        return open
          ? {
              "aria-autocomplete": "list",
              "aria-controls": id,
              "aria-activedescendant": `${id}-${open.active}`,
            }
          : { "aria-autocomplete": "list" };
      },
    ),
  ],
});

export const completionOpen = (state: EditorState): boolean =>
  !!state.field(completionState, false);

function accept(view: EditorView, index?: number): boolean {
  const open = view.state.field(completionState);
  if (!open) return false;
  const option = open.list.options[index ?? open.active];
  const head = view.state.selection.main.head;
  view.dispatch({
    changes: { from: open.list.from, to: head, insert: option.apply },
    selection: EditorSelection.cursor(
      open.list.from + (option.caret ?? option.apply.length),
    ),
    effects: setOpen.of(null),
    userEvent: "input.complete",
  });
  return true;
}

function move(view: EditorView, by: number): boolean {
  const open = view.state.field(completionState);
  if (!open) return false;
  const count = open.list.options.length;
  view.dispatch({
    effects: setOpen.of({
      ...open,
      active: (open.active + by + count) % count,
    }),
  });
  return true;
}

const tooltips = new Map<number, Tooltip>();
function tooltip(from: number): Tooltip {
  let found = tooltips.get(from);
  if (!found) {
    found = { pos: from, above: false, create: (view) => listView(view) };
    tooltips.clear();
    tooltips.set(from, found);
  }
  return found;
}

function listView(view: EditorView): TooltipView {
  const list = document.createElement("ul");
  list.className = "cm-formula-completions";
  list.setAttribute("role", "listbox");
  const render = (state: EditorState) => {
    const open = state.field(completionState);
    const id = state.facet(completionConfig).listId();
    list.id = id;
    list.setAttribute("aria-label", "Suggestions");
    list.replaceChildren();
    open?.list.options.forEach((option, i) => {
      const item = document.createElement("li");
      item.id = `${id}-${i}`;
      item.setAttribute("role", "option");
      item.setAttribute("aria-selected", String(i === open.active));
      const label = document.createElement("span");
      label.className = "cm-formula-option-label";
      label.textContent = option.label;
      item.append(label);
      if (option.detail) {
        const detail = document.createElement("span");
        detail.className = "cm-formula-option-detail";
        detail.textContent = option.detail;
        item.append(detail);
      }
      item.addEventListener("mousedown", (event) => {
        event.preventDefault();
        accept(view, i);
      });
      list.append(item);
      if (i === open.active)
        queueMicrotask(() => item.scrollIntoView({ block: "nearest" }));
    });
  };
  render(view.state);
  return { dom: list, update: (update) => render(update.state) };
}

/** Opens the list at the caret when there is something to offer. */
export function startCompletion(view: EditorView): boolean {
  const open = compute(view.state);
  view.dispatch({ effects: setOpen.of(open) });
  return !!open;
}

export function formulaCompletion(config: CompletionConfig): Extension {
  return [
    completionConfig.of(config),
    completionState,
    EditorView.focusChangeEffect.of((_state, focusing) =>
      focusing ? null : setOpen.of(null),
    ),
    Prec.highest(
      keymap.of([
        { key: "ArrowDown", run: (view) => move(view, 1) },
        { key: "ArrowUp", run: (view) => move(view, -1) },
        { key: "Enter", run: (view) => accept(view) },
        { key: "Tab", run: (view) => accept(view) },
        { key: "Mod-Space", run: startCompletion },
        { key: "Ctrl-Space", run: startCompletion },
      ]),
    ),
    Prec.highest(
      EditorView.domEventHandlers({
        keydown: (event, view) => {
          if (event.key !== "Escape" || !view.state.field(completionState))
            return false;
          event.preventDefault();
          event.stopPropagation();
          view.dispatch({ effects: setOpen.of(null) });
          return true;
        },
      }),
    ),
  ];
}
