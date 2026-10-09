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
  COMMAND_LABELS,
  KEYMAP,
  canonicalCombo,
  commandFor,
  duplicateCombos,
  eventCombo,
  formatCombo,
  shortcutSheet,
  type KeyEventLike,
  type KeyPlatform,
} from "../src/app/editor/state/keymap";

const press = (
  key: string,
  modifiers: Partial<KeyEventLike> = {},
): KeyEventLike => ({
  key,
  code: modifiers.code ?? "",
  ctrlKey: false,
  metaKey: false,
  altKey: false,
  shiftKey: false,
  ...modifiers,
});
const platforms: KeyPlatform[] = ["mac", "other"];

describe("the shortcut table", () => {
  it("writes every combination in canonical form", () => {
    for (const row of KEYMAP)
      for (const combo of row.keys)
        expect(canonicalCombo(combo), row.id).toBe(combo);
  });

  it("binds each key combination once per platform", () => {
    for (const platform of platforms)
      expect(duplicateCombos(KEYMAP, platform), platform).toEqual([]);
    expect(
      duplicateCombos(
        [
          ...KEYMAP,
          { id: "again", keys: ["N"], commands: { canvas: "zoomIn" } },
        ],
        "mac",
      ),
    ).toEqual(["N"]);
  });

  it("gives every row a command or a note, and every command a row", () => {
    const used = new Set(KEYMAP.flatMap((row) => Object.values(row.commands)));
    for (const row of KEYMAP)
      expect(
        Object.values(row.commands).some(Boolean) || !!row.note,
        row.id,
      ).toBe(true);
    for (const command of Object.keys(COMMAND_LABELS))
      expect(used.has(command as never), command).toBe(true);
  });
});

describe("what a key does where focus is", () => {
  it("runs one Ctrl/Cmd+Enter binding with a meaning per surface", () => {
    const options = { platform: "mac" as const };
    expect(commandFor("Mod+Enter", "canvas", options)).toBe("executeWorkflow");
    expect(commandFor("Mod+Enter", "stepDetails", options)).toBe("executeStep");
    expect(
      commandFor("Mod+Enter", ["formula", "stepDetails"], {
        ...options,
        typing: true,
      }),
    ).toBe("executeStep");
    expect(commandFor("Mod+Enter", "decisionGrid", options)).toBeNull();
  });

  it("acts while typing only for save, execute, undo, redo and moving between regions", () => {
    const typing = { platform: "other" as const, typing: true };
    expect(commandFor("N", "canvas", typing)).toBeNull();
    expect(commandFor("Mod+S", "stepDetails", typing)).toBe("save");
    expect(commandFor("Mod+Z", "stepDetails", typing)).toBe("undo");
    expect(commandFor("Mod+Z", ["formula", "stepDetails"], typing)).toBeNull();
    expect(commandFor("=", ["fixedField", "stepDetails"], typing)).toBe(
      "switchToMapped",
    );
    expect(commandFor("=", "stepDetails", typing)).toBeNull();
    expect(commandFor("=", "canvas", { platform: "other" })).toBe("zoomIn");
    expect(commandFor("F6", ["fixedField", "stepDetails"], typing)).toBe(
      "nextRegion",
    );
    expect(commandFor("Shift+F6", "stepDetails", typing)).toBe(
      "previousRegion",
    );
  });

  it("binds the step details keys once", () => {
    const at = { platform: "mac" as const };
    expect(commandFor("F6", "stepDetails", at)).toBe("nextRegion");
    expect(commandFor("Shift+F6", "stepDetails", at)).toBe("previousRegion");
    expect(commandFor("/", "stepDetails", at)).toBe("searchPane");
    expect(commandFor("F2", "stepDetails", at)).toBe("renameStep");
    expect(commandFor("Alt+ArrowUp", "stepDetails", at)).toBe("moveRowUp");
    expect(commandFor("Alt+ArrowDown", "stepDetails", at)).toBe("moveRowDown");
    expect(commandFor("Enter", "stepDetails", at)).toBe("mapTo");
    expect(commandFor("Mod+Alt+Shift+ArrowRight", "stepDetails", at)).toBe(
      "nextStepDetails",
    );
    expect(commandFor("Alt+ArrowUp", "decisionGrid", at)).toBe("moveRuleUp");
    const steps = shortcutSheet("mac").find((s) => s.title === "Step details");
    expect(steps?.entries).toContainEqual({
      keys: ["F6"],
      label: "Next region",
    });
  });

  it("redoes with Ctrl+Y on Windows and Linux only", () => {
    expect(commandFor("Mod+Y", "canvas", { platform: "other" })).toBe("redo");
    expect(commandFor("Mod+Y", "canvas", { platform: "mac" })).toBeNull();
  });

  it("leaves D unassigned and Ctrl/Cmd+K reserved", () => {
    expect(commandFor("D", "canvas", { platform: "mac" })).toBeNull();
    expect(commandFor("Mod+K", "canvas", { platform: "mac" })).toBeNull();
  });
});

describe("key presses", () => {
  it("reads Option combinations on macOS by the physical key", () => {
    expect(eventCombo(press("≈", { altKey: true, code: "KeyX" }), "mac")).toBe(
      "Alt+X",
    );
    expect(
      eventCombo(
        press("ˇ", { altKey: true, shiftKey: true, code: "KeyT" }),
        "mac",
      ),
    ).toBe("Alt+Shift+T");
  });

  it("keeps Shift out of typed symbols and in letters", () => {
    expect(eventCombo(press("?", { shiftKey: true }), "other")).toBe("?");
    expect(eventCombo(press("+", { shiftKey: true }), "other")).toBe("+");
    expect(eventCombo(press("S", { shiftKey: true }), "other")).toBe("Shift+S");
    expect(eventCombo(press("n"), "other")).toBe("N");
  });

  it("maps Ctrl to Mod on Windows and Linux and Cmd to Mod on macOS", () => {
    expect(eventCombo(press("z", { ctrlKey: true }), "other")).toBe("Mod+Z");
    expect(
      eventCombo(press("z", { metaKey: true, shiftKey: true }), "mac"),
    ).toBe("Mod+Shift+Z");
    expect(eventCombo(press("z", { ctrlKey: true }), "mac")).toBe("Ctrl+Z");
    expect(
      eventCombo(
        press("ArrowLeft", { metaKey: true, altKey: true, shiftKey: true }),
        "mac",
      ),
    ).toBe("Mod+Alt+Shift+ArrowLeft");
  });
});

describe("the shortcuts sheet", () => {
  it("lists every bound row and the differences from n8n", () => {
    const sheet = shortcutSheet("mac");
    const labels = sheet.flatMap((s) => s.entries.map((e) => e.label));
    for (const row of KEYMAP.filter((r) => r.platform !== "other"))
      for (const command of Object.values(row.commands))
        if (command) expect(labels, row.id).toContain(COMMAND_LABELS[command]);
    const differences = sheet.find((s) => s.title === "Differences from n8n");
    expect(differences?.entries.map((e) => e.keys)).toContainEqual(["D"]);
    expect(differences?.entries.map((e) => e.keys)).toContainEqual(["Cmd+K"]);
    const canvas = sheet.find((s) => s.title === "Canvas");
    expect(canvas?.entries).toContainEqual({
      keys: ["+", "="],
      label: "Zoom in",
    });
  });

  it("writes keys the way each platform shows them", () => {
    expect(formatCombo("Mod+Shift+Z", "mac")).toBe("Cmd+Shift+Z");
    expect(formatCombo("Mod+Shift+Z", "other")).toBe("Ctrl+Shift+Z");
    expect(formatCombo("Alt+X", "mac")).toBe("Option+X");
    expect(formatCombo("Mod+Alt+Shift+ArrowLeft", "other")).toBe(
      "Ctrl+Alt+Shift+←",
    );
    expect(formatCombo("+", "mac")).toBe("+");
  });

  it("reads each difference from n8n as two sentences", () => {
    const differences = (platform: KeyPlatform) =>
      shortcutSheet(platform).find((s) => s.title === "Differences from n8n")
        ?.entries;
    const entries = (mod: string) => [
      {
        keys: ["/"],
        label:
          "Search in the Add a step panel. Replaces A, which opens the step list in n8n.",
      },
      { keys: ["F9"], label: "Toggle a breakpoint. Not in n8n." },
      { keys: ["?"], label: "Show keyboard shortcuts. Not in n8n." },
      {
        keys: [`${mod}+K`],
        label: "Reserved for a command bar. Opens the command bar in n8n.",
      },
      {
        keys: ["D"],
        label:
          "Not assigned: Weave has no deactivated steps. Deactivates a step in n8n.",
      },
    ];
    expect(differences("mac")).toEqual(entries("Cmd"));
    expect(differences("other")).toEqual(entries("Ctrl"));
  });
});
