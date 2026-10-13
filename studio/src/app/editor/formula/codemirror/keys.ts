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
// The formula editor's keys. Undo and Redo are the workflow's, as in every
// field of step details; Enter adds a line only in multiline fields;
// Backspace and Delete remove a pill whole; Escape asks the field first.
import {
  EditorSelection,
  EditorState,
  type ChangeSpec,
  type Extension,
} from "@codemirror/state";
import { EditorView, keymap } from "@codemirror/view";
import { formulaState } from "./decorations";

export interface KeyConfig {
  multiline: boolean;
  undo(): void;
  redo(): void;
  /** True when the field used the key (an unapplied formula reverted). */
  escape(event: KeyboardEvent): boolean;
}

function removePill(view: EditorView, direction: -1 | 1): boolean {
  const { main } = view.state.selection;
  if (!main.empty) return false;
  const span = view.state
    .field(formulaState)
    .spans.find((item) =>
      direction < 0 ? item.to === main.head : item.from === main.head,
    );
  if (!span) return false;
  view.dispatch({
    changes: { from: span.from, to: span.to },
    selection: EditorSelection.cursor(span.from),
    userEvent: "delete",
  });
  return true;
}

/** Text that arrives with line breaks (paste, drop) stays on one line. */
export const singleLine: Extension = EditorState.transactionFilter.of(
  (transaction) => {
    if (!transaction.docChanged) return transaction;
    let breaks = false;
    transaction.changes.iterChanges((_fromA, _toA, _fromB, _toB, inserted) => {
      if (inserted.lines > 1) breaks = true;
    });
    if (!breaks) return transaction;
    const changes: ChangeSpec[] = [];
    transaction.changes.iterChanges((fromA, toA, _fromB, _toB, inserted) =>
      changes.push({
        from: fromA,
        to: toA,
        insert: inserted.toString().replace(/\n/g, " "),
      }),
    );
    return { changes, selection: transaction.selection, scrollIntoView: true };
  },
);

export function formulaKeys(config: KeyConfig): Extension {
  const lineBreak = (view: EditorView): boolean => {
    if (config.multiline) view.dispatch(view.state.replaceSelection("\n"));
    return true;
  };
  return [
    keymap.of([
      { key: "Mod-z", run: () => (config.undo(), true) },
      { key: "Mod-Shift-z", run: () => (config.redo(), true) },
      { key: "Mod-y", run: () => (config.redo(), true) },
      {
        // Shift+Enter too: left to the browser it would break the line.
        key: "Enter",
        run: lineBreak,
        shift: lineBreak,
      },
      { key: "Backspace", run: (view) => removePill(view, -1) },
      { key: "Delete", run: (view) => removePill(view, 1) },
    ]),
    EditorView.domEventHandlers({
      keydown: (event) => {
        if (event.key !== "Escape" || !config.escape(event)) return false;
        event.preventDefault();
        event.stopPropagation();
        return true;
      },
    }),
  ];
}
