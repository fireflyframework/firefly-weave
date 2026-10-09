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
import { describe, expect, it } from "vitest";
import {
  canvasCommand,
  canvasSheet,
  keyPlatform,
} from "../src/app/editor/state/canvas-commands";
import {
  EDITOR_NAV_KEY,
  EDITOR_NAV_WIDE,
  MINIMAP_KEY,
  editorNavExpanded,
  minimapPinned,
  setEditorNavExpanded,
  setMinimapPinned,
} from "../src/app/editor/state/canvas-preferences";
import type { KeyEventLike } from "../src/app/editor/state/keymap";

const press = (
  key: string,
  extra: Partial<KeyEventLike> = {},
): KeyEventLike => ({
  key,
  code: "",
  ctrlKey: false,
  metaKey: false,
  altKey: false,
  shiftKey: false,
  ...extra,
});
const on = { platform: "other" as const, typing: false, onStep: true };

describe("canvas keys", () => {
  it("run the canvas rows of the shortcut table", () => {
    expect(canvasCommand(press("1"), on)).toBe("fitView");
    expect(canvasCommand(press("+", { shiftKey: true }), on)).toBe("zoomIn");
    expect(canvasCommand(press("n"), on)).toBe("openAddStep");
    expect(canvasCommand(press("/"), on)).toBe("searchAddStep");
    expect(canvasCommand(press("ArrowLeft", { shiftKey: true }), on)).toBe(
      "extendUpstream",
    );
    expect(
      canvasCommand(press("z", { metaKey: true }), { ...on, platform: "mac" }),
    ).toBe("undo");
    expect(canvasCommand(press("y", { ctrlKey: true }), on)).toBe("redo");
    expect(canvasCommand(press("Escape"), on)).toBe("escape");
    expect(canvasCommand(press("?", { shiftKey: true }), on)).toBe(
      "showShortcuts",
    );
  });

  it("leave rows for features the canvas doesn't have yet to those features", () => {
    for (const key of [
      press("p"),
      press("c", { ctrlKey: true }),
      press("v", { ctrlKey: true }),
      press("Enter", { ctrlKey: true }),
      press("S", { shiftKey: true }),
      press("F9"),
      press("l"),
      press("r"),
    ])
      expect(canvasCommand(key, on), key.key).toBeNull();
  });

  it("act on steps only from a step or the canvas, never from a + or a tool", () => {
    const fromPlus = { ...on, onStep: false };
    expect(canvasCommand(press("Delete"), fromPlus)).toBeNull();
    expect(canvasCommand(press("Enter"), fromPlus)).toBeNull();
    expect(canvasCommand(press("ArrowRight"), fromPlus)).toBeNull();
    expect(canvasCommand(press("d", { ctrlKey: true }), fromPlus)).toBeNull();
    expect(canvasCommand(press("Delete"), on)).toBe("deleteSelection");
    expect(canvasCommand(press("-"), fromPlus)).toBe("zoomOut");
  });

  it("keep typing in a field to the field, except save and undo", () => {
    const typing = { ...on, typing: true };
    expect(canvasCommand(press("n"), typing)).toBeNull();
    expect(canvasCommand(press("s", { ctrlKey: true }), typing)).toBe("save");
    expect(canvasCommand(press("z", { ctrlKey: true }), typing)).toBe("undo");
  });

  it("read the platform from the browser", () => {
    expect(keyPlatform("MacIntel")).toBe("mac");
    expect(keyPlatform("iPad")).toBe("mac");
    expect(keyPlatform("Win32")).toBe("other");
    expect(keyPlatform("Linux x86_64")).toBe("other");
  });

  it("list only what the canvas does on its sheet", () => {
    const sheet = canvasSheet("mac");
    expect(sheet.map((section) => section.title)).toEqual([
      "Everywhere",
      "Canvas",
      "Differences from n8n",
    ]);
    expect(sheet[0].entries.map((entry) => entry.label)).toEqual([
      "Undo",
      "Redo",
      "Save the draft, or save to file",
      "Close, cancel, then clear the selection",
    ]);
    const canvas = sheet[1].entries.map((entry) => entry.label);
    for (const label of [
      "Fit view",
      "Open step details",
      "Extend the selection downstream",
      "Select steps with a box",
    ])
      expect(canvas).toContain(label);
    for (const label of [
      "Pin output or unpin",
      "Copy",
      "Tidy layout",
      "Add a sticky note",
      "Execute workflow",
      "Toggle a breakpoint",
    ])
      expect(canvas).not.toContain(label);
  });

  it("keep the minimap choice under the ui: prefix", () => {
    const values = new Map<string, string>();
    const storage = () => ({
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => void values.set(key, value),
    });
    expect(MINIMAP_KEY).toBe("ui:weave.canvas.minimap");
    expect(minimapPinned(storage)).toBe(false);
    expect(setMinimapPinned(true, storage)).toBe(true);
    expect(minimapPinned(storage)).toBe(true);
    expect(
      minimapPinned(() => {
        throw new Error("blocked");
      }),
    ).toBe(false);
    expect(setMinimapPinned(true, () => null)).toBe(false);
  });

  it("open the editor's navigation expanded from 1440 px unless the viewer chose", () => {
    const values = new Map<string, string>();
    const storage = () => ({
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => void values.set(key, value),
    });
    expect(EDITOR_NAV_KEY).toBe("ui:weave.editor.navExpanded");
    expect(EDITOR_NAV_WIDE).toBe(1440);
    expect(editorNavExpanded(1440, storage)).toBe(true);
    expect(editorNavExpanded(1439, storage)).toBe(false);
    expect(editorNavExpanded(1920, storage)).toBe(true);
    expect(editorNavExpanded(1280, storage)).toBe(false);
    // A stored choice wins at every width.
    values.set(EDITOR_NAV_KEY, "false");
    expect(editorNavExpanded(1920, storage)).toBe(false);
    values.set(EDITOR_NAV_KEY, "true");
    expect(editorNavExpanded(1280, storage)).toBe(true);
    // Anything else stored is no choice.
    values.set(EDITOR_NAV_KEY, "wide");
    expect(editorNavExpanded(1440, storage)).toBe(true);
    expect(editorNavExpanded(1280, storage)).toBe(false);
  });

  it("save the editor's navigation choice and fall back to the width when storage refuses", () => {
    const values = new Map<string, string>();
    const storage = () => ({
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => void values.set(key, value),
    });
    expect(setEditorNavExpanded(true, storage)).toBe(true);
    expect(values.get("ui:weave.editor.navExpanded")).toBe("true");
    expect(setEditorNavExpanded(false, storage)).toBe(true);
    expect(values.get("ui:weave.editor.navExpanded")).toBe("false");
    const blocked = () => {
      throw new Error("blocked");
    };
    expect(editorNavExpanded(1440, blocked)).toBe(true);
    expect(editorNavExpanded(1280, blocked)).toBe(false);
    expect(setEditorNavExpanded(true, blocked)).toBe(false);
    expect(setEditorNavExpanded(true, () => null)).toBe(false);
  });
});
