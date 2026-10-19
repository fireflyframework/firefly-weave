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
import {
  EditorView,
  runScopeHandlers,
  type DecorationSet,
} from "@codemirror/view";
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
import {
  formulaKeys,
  singleLine,
} from "../src/app/editor/formula/codemirror/keys";
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

/**
 * Backspace and Delete through the editor's own keymap, with no browser:
 * engines that don't delete a non-editable pill whole on their own (WebKit
 * in the desktop app) rely on these keys and on the pills being atomic.
 */
describe("the formula editor's pill keys", () => {
  function keyed(doc: string, caret: number) {
    const base = EditorState.create({
      doc,
      selection: { anchor: caret },
      extensions: [
        decorationConfig.of({
          mode: "formula",
          look,
          pick: () => undefined,
          error: () => null,
        }),
        formulaState,
        formulaKeys({
          multiline: false,
          undo: () => undefined,
          redo: () => undefined,
          escape: () => false,
        }),
      ],
    });
    // The keymap and the pill commands read the state and dispatch.
    const view = {
      state: base,
      dispatch(spec: Parameters<EditorState["update"]>[0]) {
        view.state = view.state.update(spec).state;
      },
    };
    const press = (key: "Backspace" | "Delete") =>
      runScopeHandlers(
        view as unknown as EditorView,
        {
          type: "keydown",
          key,
          keyCode: key === "Backspace" ? 8 : 46,
          altKey: false,
          ctrlKey: false,
          metaKey: false,
          shiftKey: false,
        } as KeyboardEvent,
        "editor",
      );
    return { view, press };
  }

  it("removes a whole reference with Backspace at its end", () => {
    const { view, press } = keyed("input.amount > 1", 12);
    expect(press("Backspace")).toBe(true);
    expect(view.state.doc.toString()).toBe(" > 1");
    expect(view.state.selection.main.head).toBe(0);
  });
  it("removes a whole reference with Delete just before it, inside a formula", () => {
    const { view, press } = keyed("1 < input.amount and true", 4);
    expect(press("Delete")).toBe(true);
    expect(view.state.doc.toString()).toBe("1 <  and true");
    expect(view.state.selection.main.head).toBe(4);
  });
  it("leaves other characters to the browser", () => {
    const { view, press } = keyed("input.amount > 1", 16);
    expect(press("Backspace")).toBe(false);
    expect(view.state.doc.toString()).toBe("input.amount > 1");
  });
  it("makes every reference atomic, so the caret steps over it whole", () => {
    const { view } = keyed("1 < input.amount and steps.check.output", 0);
    const ranges: string[] = [];
    for (const atomic of view.state.facet(EditorView.atomicRanges))
      atomic(view as unknown as EditorView).between(0, 1e9, (from, to) => {
        ranges.push(`${from}-${to}`);
      });
    expect(ranges).toEqual(["4-16", "21-39"]);
  });
});

/**
 * Undo and Redo go to the workflow, never to the editor's own text. Chromium
 * on Linux can report Ctrl+Shift+Z with a lowercase "z" (Caps Lock, or an
 * automated key press); that is still Redo, not Undo.
 */
describe("the formula editor's undo and redo keys", () => {
  function keyed(platform: "mac" | "other") {
    const calls: string[] = [];
    const state = EditorState.create({
      doc: "First {{ input.email }}",
      extensions: [
        formulaKeys({
          multiline: true,
          platform,
          undo: () => calls.push("undo"),
          redo: () => calls.push("redo"),
          escape: () => false,
        }),
      ],
    });
    const view = { state, dispatch: () => undefined };
    const press = (
      key: string,
      modifiers: { ctrl?: boolean; meta?: boolean; shift?: boolean },
    ) =>
      runScopeHandlers(
        view as unknown as EditorView,
        {
          type: "keydown",
          key,
          code: `Key${key.toUpperCase()}`,
          keyCode: key.toUpperCase().charCodeAt(0),
          altKey: false,
          ctrlKey: !!modifiers.ctrl,
          metaKey: !!modifiers.meta,
          shiftKey: !!modifiers.shift,
        } as KeyboardEvent,
        "editor",
      );
    return { calls, press };
  }

  it("redoes with Ctrl+Shift+Z off macOS, whatever case the key reports", () => {
    const { calls, press } = keyed("other");
    expect(press("z", { ctrl: true })).toBe(true);
    expect(press("z", { ctrl: true, shift: true })).toBe(true);
    expect(press("Z", { ctrl: true, shift: true })).toBe(true);
    expect(press("y", { ctrl: true })).toBe(true);
    expect(calls).toEqual(["undo", "redo", "redo", "redo"]);
  });
  it("redoes with Cmd+Shift+Z on macOS, and leaves Ctrl+Z to the system", () => {
    const { calls, press } = keyed("mac");
    expect(press("z", { meta: true })).toBe(true);
    expect(press("z", { meta: true, shift: true })).toBe(true);
    expect(press("Z", { meta: true, shift: true })).toBe(true);
    expect(press("z", { ctrl: true })).toBe(false);
    expect(calls).toEqual(["undo", "redo", "redo"]);
  });
});
