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
import type { DecorationSet } from "@codemirror/view";
import { describe, expect, it } from "vitest";
import {
  decorationConfig,
  formulaState,
  refreshDecorations,
  type PillLook,
} from "../src/app/editor/formula/codemirror/decorations";
import { singleLine } from "../src/app/editor/formula/codemirror/keys";

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

  it("draws pills between a template's braces and leaves the text around them plain", () => {
    const { state } = editorState("Hi {{ input.name }}!", null, "template");
    expect(classes(state.field(formulaState).decorations)).toEqual([
      "3-5:cm-formula-brace",
      "6-16:PillWidget",
      "17-19:cm-formula-brace",
    ]);
  });
});
