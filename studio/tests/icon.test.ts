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
import { describe, expect, it } from "vitest";
import { iconPaths, iconSize, iconStrokes, ringedIcons } from "../src/app/icon";
import { kinds } from "../src/app/model";

describe("icons", () => {
  it("draws every meaning differently", () => {
    const drawings = new Map<string, string>();
    for (const [name, path] of Object.entries(iconPaths)) {
      // A ring is part of the drawing.
      const drawing = `${path}|${ringedIcons.has(name)}`;
      expect(drawings.get(drawing), `${name} reuses a drawing`).toBeUndefined();
      drawings.set(drawing, name);
    }
  });

  it("has the icons the plan adds and fixes", () => {
    for (const name of [
      "upload",
      "workspace",
      "more",
      "help",
      "failCircle",
      "settings",
      "signal",
      "humanTask",
    ])
      expect(iconPaths[name], name).toBeTruthy();
    // Wait for signal no longer borrows the email envelope.
    expect(iconPaths["signal"]).not.toBe(iconPaths["email"]);
    // Import (upload) and Export (download) point different ways.
    expect(iconPaths["upload"]).not.toBe(iconPaths["download"]);
    // The settings gear has its own outline, not a ring with spokes.
    expect(ringedIcons.has("settings")).toBe(false);
    expect(iconPaths["settings"]).toMatch(/^M12 15a3 3 0 1 0 0-6/);
    // The person's head is a whole circle (two half arcs), not half of one.
    expect(iconPaths["humanTask"]).toMatch(
      /^M12 2a3 3 0 1 0 0 6 3 3 0 0 0 0-6z/,
    );
    for (const name of ["help", "failCircle", "runs", "wait"])
      expect(ringedIcons.has(name), name).toBe(true);
  });

  it("has an icon for every step kind", () => {
    for (const kind of kinds) expect(iconPaths[kind], kind).toBeTruthy();
  });

  it("comes in two sizes with their own stroke", () => {
    expect(iconStrokes).toEqual({ 16: 1.5, 20: 1.75 });
    expect(iconSize(16)).toBe(16);
    expect(iconSize("16")).toBe(16);
    expect(iconSize("20")).toBe(20);
    expect(iconSize(undefined)).toBeUndefined();
    expect(iconSize("18" as "16")).toBeUndefined();
  });
});
