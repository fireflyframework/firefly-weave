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
// The output editor's text field: CodeMirror with Studio's JSON colors and
// its own small Undo. Ctrl/Cmd+Z and Ctrl/Cmd+Shift+Z (or Ctrl+Y) undo and
// redo the typing in this field, never the workflow; Ctrl/Cmd+Enter saves;
// Escape asks the editor first; Ctrl/Cmd+S goes on to Studio's Save. It
// loads with the formula editor's chunk (@defer), never on the first page.
import {
  Annotation,
  EditorSelection,
  EditorState,
  RangeSetBuilder,
} from "@codemirror/state";
import {
  Decoration,
  EditorView,
  ViewPlugin,
  keymap,
  type DecorationSet,
  type ViewUpdate,
} from "@codemirror/view";
import { eventCombo, KEY_PLATFORM, type KeyPlatform } from "../../state/keymap";
import { jsonTokens } from "../../ndv/panes/json-tokens";

export interface JsonEditorOptions {
  text: string;
  label: string;
  describedBy: string;
  onChange(text: string): void;
  onSave(): void;
  /** True when the editor used the key (it closed, say); otherwise it goes on to step details. */
  onEscape(): boolean;
  /** Which modifier is Mod; the platform Studio runs on unless a test says. */
  platform?: KeyPlatform;
}
export interface JsonEditor {
  view: EditorView;
  text(): string;
  focus(): void;
  destroy(): void;
}

const marks = {
  key: Decoration.mark({ class: "cm-json-key" }),
  string: Decoration.mark({ class: "cm-json-string" }),
  number: Decoration.mark({ class: "cm-json-number" }),
  literal: Decoration.mark({ class: "cm-json-literal" }),
};
function colors(text: string): DecorationSet {
  const builder = new RangeSetBuilder<Decoration>();
  // A mark decoration is never empty.
  for (const token of jsonTokens(text))
    if (token.to > token.from)
      builder.add(token.from, token.to, marks[token.kind]);
  return builder.finish();
}
const jsonColors = ViewPlugin.fromClass(
  class {
    decorations: DecorationSet;
    constructor(view: EditorView) {
      this.decorations = colors(view.state.doc.toString());
    }
    update(update: ViewUpdate) {
      if (update.docChanged)
        this.decorations = colors(update.state.doc.toString());
    }
  },
  { decorations: (plugin) => plugin.decorations },
);

const jsonTheme = EditorView.theme({
  "&": {
    color: "var(--text)",
    backgroundColor: "transparent",
    font: "var(--type-mono)",
    height: "100%",
  },
  "&.cm-focused": { outline: "none" },
  ".cm-scroller": { fontFamily: "inherit", lineHeight: "inherit" },
  ".cm-content": { padding: "0", caretColor: "var(--text)" },
  ".cm-line": { padding: "0" },
  ".cm-json-key": { color: "var(--syntax-keyword)" },
  ".cm-json-string": { color: "var(--syntax-string)" },
  ".cm-json-number, .cm-json-literal": { color: "var(--syntax-number)" },
});

/** A change the editor's own Undo made: not recorded as typing. */
const fromHistory = Annotation.define<boolean>();
interface Moment {
  text: string;
  anchor: number;
  head: number;
}
const LIMIT = 100;
const MERGE_MS = 600;

export function createJsonEditor(
  parent: HTMLElement,
  options: JsonEditorOptions,
): JsonEditor {
  const platform = options.platform ?? KEY_PLATFORM;
  const past: Moment[] = [];
  const future: Moment[] = [];
  let lastTyped = -Infinity;
  const moment = (state: EditorState): Moment => ({
    text: state.doc.toString(),
    anchor: state.selection.main.anchor,
    head: state.selection.main.head,
  });
  const restore = (view: EditorView, to: Moment) =>
    view.dispatch({
      changes: { from: 0, to: view.state.doc.length, insert: to.text },
      selection: EditorSelection.range(
        Math.min(to.anchor, to.text.length),
        Math.min(to.head, to.text.length),
      ),
      annotations: fromHistory.of(true),
      scrollIntoView: true,
    });
  const travel = (view: EditorView, from: Moment[], to: Moment[]) => {
    const target = from.pop();
    if (!target) return;
    to.push(moment(view.state));
    lastTyped = -Infinity;
    restore(view, target);
  };
  const keys = (event: KeyboardEvent, view: EditorView): boolean => {
    const combo = eventCombo(event, platform);
    if (combo === "Mod+Z") travel(view, past, future);
    else if (combo === "Mod+Shift+Z" || combo === "Mod+Y")
      travel(view, future, past);
    else if (combo === "Mod+Enter") options.onSave();
    else return false;
    return true;
  };
  const view = new EditorView({
    parent,
    state: EditorState.create({
      doc: options.text,
      extensions: [
        EditorView.lineWrapping,
        EditorView.contentAttributes.of({
          role: "textbox",
          "aria-multiline": "true",
          "aria-label": options.label,
          "aria-describedby": options.describedBy,
          spellcheck: "false",
          autocapitalize: "off",
          autocorrect: "off",
        }),
        jsonColors,
        jsonTheme,
        // Read from the key, not CodeMirror's names (Chromium off macOS can
        // report Ctrl+Shift+Z as a lowercase "z").
        keymap.of([{ any: (target, event) => keys(event, target) }]),
        EditorView.updateListener.of((update) => {
          if (!update.docChanged) return;
          const undoing = update.transactions.some((transaction) =>
            transaction.annotation(fromHistory),
          );
          if (!undoing) {
            const now = Date.now();
            // Typing within 600 ms of the last change is one step of Undo.
            if (now - lastTyped > MERGE_MS) {
              past.push(moment(update.startState));
              if (past.length > LIMIT) past.shift();
            }
            lastTyped = now;
            future.length = 0;
          }
          options.onChange(update.state.doc.toString());
        }),
        EditorView.domEventHandlers({
          keydown: (event) => {
            if (event.key !== "Escape" || !options.onEscape()) return false;
            event.preventDefault();
            event.stopPropagation();
            return true;
          },
        }),
      ],
    }),
  });
  return {
    view,
    text: () => view.state.doc.toString(),
    focus: () => view.focus(),
    destroy: () => view.destroy(),
  };
}
