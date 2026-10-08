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
import "@angular/compiler";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  iconDrawing,
  iconNames,
  iconSize,
  iconStrokes,
  isIconName,
} from "../src/app/icon";
import { iconNodes, lucideNames } from "../src/app/icon-data";
import { kinds } from "../src/app/model";
import { iconMap, renderIconData } from "../scripts/build-icons.mjs";

const app = fileURLToPath(new URL("../src/app", import.meta.url));

function sources(dir = app): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return sources(path);
    return /\.(ts|html)$/.test(name) ? [path] : [];
  });
}

/** Every icon name a template or the code asks for, with where it is asked. */
function requestedIcons(): Map<string, string> {
  const found = new Map<string, string>();
  const add = (name: string, where: string) => {
    if (!found.has(name)) found.set(name, where);
  };
  for (const file of sources()) {
    const text = readFileSync(file, "utf8");
    const where = relative(app, file);
    for (const [tag] of text.matchAll(/<weave-icon\b[\s\S]*?\/?>/g)) {
      for (const [, name] of tag.matchAll(/\sname="([^"]+)"/g))
        add(name, where);
      // In a binding, the names are the literals a ternary or ?? can return.
      for (const [, binding] of tag.matchAll(/\[name\]="([^"]+)"/g))
        for (const [, name] of binding.matchAll(/(?:^|[?:])\s*'([^']+)'/g))
          add(name, where);
    }
    // Inputs and data that name an icon: icon="…", icon: "…", input("…").
    for (const [, name] of text.matchAll(
      /\bicon(?:="|:\s*"|\s*=\s*input\(")([A-Za-z]+)"/g,
    ))
      add(name, where);
    // Getters and maps named for icons.
    for (const [, body] of text.matchAll(
      /\bget \w*Icon\(\)\s*\{([\s\S]*?)\n {2}\}/g,
    ))
      for (const [, name] of body.matchAll(/return "([A-Za-z]+)"/g))
        add(name, where);
    for (const [, body] of text.matchAll(
      /\bconst icons\b[^=]*=\s*\{([\s\S]*?)\};/g,
    ))
      for (const [, name] of body.matchAll(/:\s*"([A-Za-z]+)"/g))
        add(name, where);
  }
  // Sidebar items draw the icon named after their view.
  const shell = readFileSync(join(app, "app.ts"), "utf8");
  const nav = /\bnav: \{ id: View; label: string \}\[\] = \[([\s\S]*?)\];/.exec(
    shell,
  );
  for (const [, id] of nav![1].matchAll(/id: "([A-Za-z]+)"/g))
    add(id, "app.ts nav");
  return found;
}

describe("icons", () => {
  it("draws the 94 mapped names from Lucide", () => {
    expect(iconNames).toHaveLength(94);
    expect(lucideNames).toEqual(iconMap);
    for (const name of iconNames)
      expect(iconNodes[name].length, name).toBeGreaterThan(0);
  });

  it("matches a fresh generation from lucide-static byte for byte", async () => {
    // A Windows checkout may turn LF into CRLF; the generator writes LF.
    const committed = readFileSync(
      new URL("../src/app/icon-data.ts", import.meta.url),
      "utf8",
    ).replace(/\r\n/g, "\n");
    expect(await renderIconData()).toBe(committed);
  });

  it("gives every Weave name its own Lucide icon", () => {
    const owner = new Map<string, string>();
    for (const [weave, lucide] of Object.entries(lucideNames)) {
      expect(owner.get(lucide), `${weave} reuses ${lucide}`).toBeUndefined();
      owner.set(lucide, weave);
    }
  });

  it("has an icon for every step kind, with the brand's mapping", () => {
    for (const kind of kinds) expect(isIconName(kind), kind).toBe(true);
    expect(lucideNames).toMatchObject({
      wait: "hourglass",
      signal: "radio-tower",
      action: "zap",
      email: "mail",
    });
  });

  it("has an icon for every name a template or the code uses", () => {
    const requested = requestedIcons();
    // The scan itself must keep finding names in each place it reads.
    for (const sentinel of [
      "plus",
      "template",
      "assistant",
      "more",
      "chevron",
      "failCircle",
      "humanTask",
      "connections",
    ])
      expect(requested.has(sentinel), sentinel).toBe(true);
    const unknown = [...requested]
      .filter(([name]) => !isIconName(name))
      .map(([name, where]) => `${name} (${where})`);
    expect(unknown).toEqual([]);
  });

  it("draws workflows for an unknown name at run time", () => {
    expect(isIconName("no-such-icon")).toBe(false);
    expect(isIconName("toString")).toBe(false);
    expect(iconDrawing("no-such-icon")).toBe(iconNodes.workflows);
  });

  it("strokes 1.5px at both sizes", () => {
    expect(iconStrokes).toEqual({ 16: 1.5, 20: 1.5 });
    expect(iconSize(16)).toBe(16);
    expect(iconSize("16")).toBe(16);
    expect(iconSize("20")).toBe(20);
    expect(iconSize(undefined)).toBeUndefined();
    expect(iconSize("18" as "16")).toBeUndefined();
  });
});
