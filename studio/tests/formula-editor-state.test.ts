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
import { EditorState } from "@codemirror/state";
import { EditorView, type DecorationSet } from "@codemirror/view";
import { describe, expect, it } from "vitest";
import {
  decorationConfig,
  formulaState,
  refreshDecorations,
  type PillLook,
} from "../src/app/editor/formula/codemirror/decorations";
import {
  completionOpen,
  formulaCompletion,
} from "../src/app/editor/formula/codemirror/completion";
import { singleLine } from "../src/app/editor/formula/codemirror/keys";
import type { CompletionList } from "../src/app/editor/formula/complete";

const look = (pointer: string): PillLook => ({
  parts: pointer.split("/").slice(1),
  icon: "source",
  title: pointer,
  name: `reference ${pointer}`,
});

function editorState(
  doc: string,
  error: { from: number; to: number } | null = null,
  mode: "formula" | "template" = "formula",
) {
  const config = { error };
  const state = EditorState.create({
    doc,
    extensions: [
      singleLine,
      decorationConfig.of({
        mode,
        look,
        pick: () => undefined,
        error: () => config.error,
      }),
      formulaState,
    ],
  });
  return { state, config };
}

function classes(set: DecorationSet): string[] {
  const found: string[] = [];
  set.between(0, 1e9, (from, to, decoration) => {
    const spec = decoration.spec as { class?: string; widget?: unknown };
    found.push(
      `${from}-${to}:${spec.class ?? (spec.widget ? spec.widget.constructor.name : "?")}`,
    );
  });
  return found;
}

describe("the formula editor's state", () => {
  it("keeps a single-line field on one line when text with line breaks arrives", () => {
    const { state } = editorState("input.a > 1");
    const next = state.update({
      changes: { from: 11, insert: "\nand\r\ninput.b" },
    }).state;
    expect(next.doc.lines).toBe(1);
    expect(next.doc.toString()).toBe("input.a > 1 and input.b");
  });

  it("draws each reference as one pill, and nothing else inside it", () => {
    const { state } = editorState('input.amount > 1 and "x" == ref("/x")');
    const drawn = state.field(formulaState);
    expect(drawn.spans).toEqual([
      { from: 0, to: 12, pointer: "/input/amount" },
      { from: 28, to: 37, pointer: "/x" },
    ]);
    expect(classes(drawn.pills)).toEqual([
      "0-12:PillWidget",
      "28-37:PillWidget",
    ]);
    expect(classes(drawn.decorations)).toEqual([
      "0-12:PillWidget",
      "13-14:cm-formula-operator",
      "15-16:cm-formula-number",
      "17-20:cm-formula-keyword",
      "21-24:cm-formula-string",
      "25-27:cm-formula-operator",
      "28-37:PillWidget",
    ]);
  });

  it("underlines the error, or marks the end with a caret, and redraws when asked", () => {
    const { state, config } = editorState("input.a >", { from: 8, to: 9 });
    expect(classes(state.field(formulaState).decorations)).toContain(
      "8-9:cm-formula-error",
    );
    config.error = { from: 9, to: 9 };
    const redrawn = state.update({
      effects: refreshDecorations.of(null),
    }).state;
    expect(classes(redrawn.field(formulaState).decorations)).toContain(
      "9-9:CaretWidget",
    );
    expect(classes(redrawn.field(formulaState).decorations)).not.toContain(
      "8-9:cm-formula-error",
    );
  });

  it("draws an error a keystroke behind the text inside the text", () => {
    const { state } = editorState("input.a >", { from: 10, to: 12 });
    expect(classes(state.field(formulaState).decorations)).toContain(
      "9-9:CaretWidget",
    );
    const shorter = editorState("input.a", { from: 8, to: 9 }).state;
    expect(classes(shorter.field(formulaState).decorations)).toContain(
      "7-7:CaretWidget",
    );
  });

  it("draws pills between a template's braces and leaves the text around them plain", () => {
    const { state } = editorState("Hi {{ input.name }}!", null, "template");
    expect(classes(state.field(formulaState).decorations)).toEqual([
      "3-5:cm-formula-brace",
      "6-16:PillWidget",
      "17-19:cm-formula-brace",
    ]);
  });
});

describe("the suggestions' state", () => {
  const source = (text: string, caret: number): CompletionList | null =>
    /input\.?$/.test(text.slice(0, caret)) || /^in$/.test(text.slice(0, caret))
      ? {
          from: 0,
          to: caret,
          options: [{ label: "input", kind: "root", apply: "input" }],
        }
      : null;
  const start = () =>
    EditorState.create({
      extensions: [
        formulaCompletion({
          source: () => source,
          listId: () => "amount-suggestions",
        }),
      ],
    });
  const type = (state: EditorState, text: string) =>
    state.update({
      changes: { from: state.doc.length, insert: text },
      selection: { anchor: state.doc.length + text.length },
      userEvent: "input.type",
    }).state;
  const attributes = (state: EditorState) =>
    Object.assign(
      {},
      ...state
        .facet(EditorView.contentAttributes)
        .map((source) => (typeof source === "function" ? {} : source)),
    ) as Record<string, string>;

  it("opens while a name is typed, names the list, and closes when nothing matches", () => {
    const closed = start();
    expect(completionOpen(closed)).toBe(false);
    expect(attributes(closed)).toEqual({ "aria-autocomplete": "list" });
    const open = type(type(closed, "i"), "n");
    expect(completionOpen(open)).toBe(true);
    expect(attributes(open)).toEqual({
      "aria-autocomplete": "list",
      "aria-controls": "amount-suggestions",
      "aria-activedescendant": "amount-suggestions-0",
    });
    expect(completionOpen(type(open, " "))).toBe(false);
  });

  it("stays closed for text that isn't typed, such as a paste", () => {
    const state = start();
    const pasted = state.update({
      changes: { from: 0, insert: "input." },
      selection: { anchor: 6 },
      userEvent: "input.paste",
    }).state;
    expect(completionOpen(pasted)).toBe(false);
  });
});
