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
import { chooseImport, readImport } from "../src/app/import-files";

const file = (name: string, size = 100) => ({ name, size });

describe("what an import holds", () => {
  it("takes one workflow file", () => {
    expect(chooseImport([file("order-intake.yaml")])).toEqual({
      workflow: 0,
      canvas: null,
    });
    expect(chooseImport([file("order-intake.json")])).toEqual({
      workflow: 0,
      canvas: null,
    });
  });

  it("takes a workflow file with its canvas file, in either order", () => {
    expect(
      chooseImport([
        file("order-intake.canvas.json"),
        file("order-intake.yml"),
      ]),
    ).toEqual({ workflow: 1, canvas: 0 });
    expect(
      chooseImport([
        file("order-intake.yaml"),
        file("order-intake.layout.json"),
      ]),
    ).toEqual({ workflow: 0, canvas: 1 });
  });

  it("says what to choose instead", () => {
    expect(chooseImport([file("order-intake.canvas.json")])).toEqual({
      error:
        "Choose the workflow file too: a canvas file opens only with its workflow.",
    });
    expect(chooseImport([file("a.yaml"), file("b.yaml")])).toEqual({
      error: "Choose one workflow file, and its canvas file if you have one.",
    });
    expect(
      chooseImport([file("a.canvas.json"), file("b.layout.json")]),
    ).toEqual({
      error: "Choose one workflow file, and its canvas file if you have one.",
    });
    expect(
      chooseImport([file("a.yaml"), file("b.yaml"), file("a.canvas.json")]),
    ).toEqual({
      error: "Choose one workflow file, and its canvas file if you have one.",
    });
    expect(chooseImport([file("notes.txt")])).toEqual({
      error: "Choose a .yaml, .yml or .json workflow file.",
    });
    expect(chooseImport([file("big.yaml", 1024 * 1024 + 1)])).toEqual({
      error: "This file is larger than 1 MiB. Choose a smaller workflow file.",
    });
    expect(
      chooseImport([file("a.yaml"), file("a.canvas.json", 262_145)]),
    ).toEqual({
      error:
        "This canvas file is larger than 256 KB. Choose the workflow file alone.",
    });
  });
});

describe("reading what was chosen", () => {
  const text = (value: string) => ({ text: async () => value });
  const unreadable = { text: async () => Promise.reject(new Error("gone")) };
  it("reads the workflow and its canvas file", async () => {
    expect(await readImport(text("kind: Workflow"), text("{}"))).toEqual({
      source: "kind: Workflow",
      canvas: "{}",
      canvasUnreadable: false,
    });
    expect(await readImport(text("kind: Workflow"), null)).toEqual({
      source: "kind: Workflow",
      canvas: null,
      canvasUnreadable: false,
    });
  });
  it("still reads the workflow when its canvas file can't be read", async () => {
    expect(await readImport(text("kind: Workflow"), unreadable)).toEqual({
      source: "kind: Workflow",
      canvas: null,
      canvasUnreadable: true,
    });
    expect(await readImport(unreadable, text("{}"))).toBeNull();
  });
});
