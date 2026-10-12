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
// The words people read in step details: the product glossary, and sentence
// case for every short label.
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * Every .ts and .html file of step details, and the canvas sidecar module.
 * The form contract check speaks to people who register forms, in the
 * language's terms, and never shows in Studio.
 */
function files(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return files(path);
    return /\.(ts|html)$/.test(name) ? [path] : [];
  });
}
const sources = [
  ...files("src/app/editor/ndv").filter(
    (path) => !path.endsWith("form-contract.ts"),
  ),
  "src/app/editor/state/canvas-sidecar.ts",
];
const withoutComments = (text: string) =>
  text
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/^\s*\/\/.*$/gm, "")
    .replace(/<!--[\s\S]*?-->/g, "");

/** The strings people read: labels, hints, messages, attributes and template text. */
function strings(): { file: string; text: string }[] {
  const found: { file: string; text: string }[] = [];
  for (const file of sources) {
    const text = withoutComments(readFileSync(file, "utf8"));
    const add = (value: string) => {
      const clean = value
        .replace(/\{\{[^}]*\}\}/g, " ")
        .replace(/\s+/g, " ")
        .trim();
      if (/[A-Za-z]{2}/.test(clean)) found.push({ file, text: clean });
    };
    for (const m of text.matchAll(
      /\b(?:label|hint|description|placeholder|addLabel|title|message|detail|text|heading|closeLabel|confirmLabel|cancelLabel):\s*"([^"]+)"/g,
    ))
      add(m[1]);
    for (const m of text.matchAll(/\b(?:notify|announce|Error)\(\s*"([^"]+)"/g))
      add(m[1]);
    // Prose anywhere in the code: labels passed by position, reasons,
    // returned messages and template literals ("Open ${id}" reads "Open X").
    // Programming errors that no one reads are left out.
    if (file.endsWith(".ts")) {
      const code = text.replace(
        /new (?:Type|Range|Syntax|Reference)Error\(\s*(?:"[^"]*"|`[^`]*`)/g,
        "",
      );
      for (const m of code.matchAll(
        /"((?:[^"\\\n]|\\.)*)"|`((?:[^`\\]|\\.)*)`/g,
      )) {
        if (m[2] !== undefined && /<[a-z]/.test(m[2])) continue;
        const literal = m[1] ?? m[2].replace(/\$\{[^}]*\}/g, "X");
        if (/^[A-Z][a-z]*(?:[ ,]|[^.?!…]*[.?!…]$)/.test(literal)) add(literal);
      }
    }
    // Markup: whole .html files, and the inline templates of .ts files.
    const markup = file.endsWith(".html")
      ? [text]
      : [...text.matchAll(/template:\s*`([\s\S]*?)`/g)].map((m) => m[1]);
    for (const part of markup) {
      for (const m of part.matchAll(
        /\s(?:aria-label|title|placeholder|heading|label|closeLabel)="([^"{]+)"/g,
      ))
        add(m[1]);
      for (const m of part.matchAll(/>([^<>{}@`]*[A-Za-z][^<>{}@`]*)</g))
        add(m[1]);
      // Words inside bindings: [attr.aria-label]="'Rename ' + name()".
      for (const m of part.matchAll(/'([A-Z][a-z][^'{}]*)'/g)) add(m[1]);
    }
  }
  return found;
}

/** Words that keep their capital inside a label: names of things, products and keys. */
const PROPER = new Set([
  "API",
  "AI",
  "JSON",
  "YAML",
  "URL",
  "HTTP",
  "HTML",
  "ID",
  "UTC",
  "SFTP",
  "Weave",
  "Studio",
  "Operate",
  "Fixed",
  "Mapped",
  "Input",
  "Output",
  "Parameters",
  "Settings",
  "Notes",
  "Docs",
  "Ctrl",
  "Cmd",
  "Alt",
  "Shift",
  "Esc",
  "Enter",
  "Option",
  "Cc",
  "Bcc",
  "GET",
  "POST",
  "PUT",
  "PATCH",
  "DELETE",
  "HEAD",
  "More",
  "Edit",
  "Simulate",
  "Execute",
  "Add",
  "Use",
  "Generate",
  "Paste",
  "Schema",
  "Table",
  "Map",
]);

describe("Words in step details", () => {
  it("reads the strings people see, wherever the code writes them", () => {
    const texts = new Set(strings().map(({ text }) => text));
    for (const known of [
      // A label passed by position, a returned reason, a title attribute,
      // template text, a template literal and a word inside a binding.
      "Stop runs after",
      "Fix the source to edit this step.",
      "Close (Esc). Changes are already saved.",
      "Fields this step can read",
      "Open X",
      "Settings, changed",
    ])
      expect(texts, known).toContain(known);
  });
  it("uses the product glossary in every new string", () => {
    const banned =
      /\b(nodes?|NDV|credentials?|expressions?|executions?|inspector)\b/i;
    const offenders = strings().filter(({ text }) => banned.test(text));
    expect(offenders).toEqual([]);
  });
  it("writes every new string in sentence case", () => {
    const labels = strings().filter(
      ({ text }) => !/[.!?…:]$/.test(text) && text.split(" ").length <= 6,
    );
    const offenders = labels.filter(({ text }) =>
      text
        .split(" ")
        .slice(1)
        .some(
          (word) =>
            /^[A-Z][a-z]/.test(word) && !PROPER.has(word.replace(/[^\w]/g, "")),
        ),
    );
    expect(offenders).toEqual([]);
  });
});
