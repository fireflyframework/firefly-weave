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
// What Operate says about transactions uses the product's words: never the
// engine's names for the parts of a transaction. The platform's own values
// ("compensating", "terminate") stay in code; only text people read is checked.
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import ts from "typescript";
import { describe, expect, it } from "vitest";

const APP = "src/app";

function tree(dir: string): string[] {
  if (!existsSync(dir)) return [];
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return tree(path);
    return name.endsWith(".ts") ? [path] : [];
  });
}

/** An inbox module about transactions, whichever separator the system joins paths with. */
const inboxTransactionModule = (path: string) =>
  /[\\/](?:incident-cases|transaction-[a-z-]+)\.ts$/.test(path);

/** The shared words, every Operate transaction module, and the inbox's transaction modules. */
const sources = () => [
  join(APP, "editor/transactions/transaction-words.ts"),
  ...tree(join(APP, "operate/transactions")),
  ...tree(join(APP, "operate/incidents")).filter(inboxTransactionModule),
];

const BANNED =
  /saga|unit of work|atomic|\bTCC\b|pivot|compensat|committing|rolled back|rollback|handler|outbox|terminate|accept reconciled|skip undo|\bnodes?\b|\bexecutions?\b|\bfacts?\b|\bbuckets?\b/i;
// "try 3 of 10" counts attempts, and "Try now" or "Try again" reads again; "try" never names a step.
const TRY = /\btry\b(?! (?:\d+|X) of (?:\d+|X)\b| (?:now|again)\b)/i;
const banned = (text: string) => BANNED.test(text) || TRY.test(text);

/** The text an inline template shows: its text nodes and its static labels. */
function templateText(html: string): string[] {
  const labels = [
    ...html.matchAll(
      /(?<![\w.[-])(?:aria-label|aria-description|title|placeholder|alt)="([^"]*)"/g,
    ),
  ].map((match) => match[1]);
  const text = html
    .replace(/<!--[\s\S]*?-->/g, "\n")
    .replace(/<[^>]*>/g, "\n")
    .replace(/\{\{[\s\S]*?\}\}/g, "X")
    .replace(/@let\b[^;]*;/g, "\n")
    .replace(
      /@(?:else if|if|else|for|switch|case|default|empty|defer|placeholder|loading)\b\s*(?:\((?:[^()]|\([^()]*\))*\))?\s*\{?/g,
      "\n",
    )
    .replace(/[{}]/g, "\n");
  return [...labels, ...text.split("\n")];
}

/**
 * The strings people read in a TypeScript source: literals with a space, or
 * starting with a capital and a lower-case letter, and the text of inline
 * templates. Identifiers, codes, types, object keys and import paths are code.
 */
function peopleStrings(file: string, source: string): string[] {
  const found: string[] = [];
  const add = (value: string) => {
    const clean = value.replace(/\s+/g, " ").trim();
    if (/[A-Za-z]{2}/.test(clean)) found.push(clean);
  };
  const visit = (node: ts.Node) => {
    const parent = node.parent;
    const code =
      parent !== undefined &&
      (ts.isLiteralTypeNode(parent) ||
        ts.isImportDeclaration(parent) ||
        ts.isExportDeclaration(parent) ||
        (ts.isPropertyAssignment(parent) && parent.name === node));
    let text: string | null = null;
    if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node))
      text = node.text;
    else if (ts.isTemplateExpression(node))
      text =
        node.head.text +
        node.templateSpans.map((span) => `X${span.literal.text}`).join("");
    if (text !== null && !code) {
      if (/<[a-z]/.test(text)) templateText(text).forEach(add);
      else if (/\s/.test(text.trim()) || /^[A-Z][a-z]/.test(text)) add(text);
    }
    if (!ts.isTemplateExpression(node)) ts.forEachChild(node, visit);
    else node.templateSpans.forEach((span) => visit(span.expression));
  };
  visit(ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true));
  return found;
}

describe("Operate's transaction words", () => {
  it("never name the engine's parts of a transaction", () => {
    const offenders = sources().flatMap((file) =>
      peopleStrings(file, readFileSync(file, "utf8"))
        .filter(banned)
        .map((text) => `${file}: ${text}`),
    );
    expect(offenders).toEqual([]);
  });

  it("read the shared words module", () => {
    const words = sources()[0];
    const texts = peopleStrings(words, readFileSync(words, "utf8"));
    for (const known of [
      "In doubt · action needed",
      "Still reserved",
      "Not undone",
      "Waiting for X, which can't be undone",
      "A step after the point of no return couldn't finish.",
    ])
      expect(texts).toContain(known);
  });

  it("scan the inbox's transaction modules on Windows paths too", () => {
    for (const separator of ["/", "\\"]) {
      const path = (name: string) =>
        ["src", "app", "operate", "incidents", name].join(separator);
      expect(inboxTransactionModule(path("incident-cases.ts"))).toBe(true);
      expect(inboxTransactionModule(path("transaction-panel.ts"))).toBe(true);
      expect(inboxTransactionModule(path("incidents-page.ts"))).toBe(false);
      expect(inboxTransactionModule(path("my-incident-cases.ts"))).toBe(false);
    }
  });

  it("find a banned word in a label, a template or its title, and leave code alone", () => {
    const source = [
      'import { x } from "./compensation";',
      'type S = "compensating" | "committing";',
      'const codes = { "WV-TXN-AFTER-PIVOT-STUCK": "A step after the point of no return couldn\'t finish." };',
      'if (state === "terminate") label = "Compensate the step";',
      "const row = `Undo of ${step} (try ${attempt} of ${of})`;",
      'const verbs = ["Try now", "Try again", "Try the step"];',
      'const template = `<p title="Rollback" [attr.data-state]="\'compensating\'">Undo {{ step }}</p>',
      "@if (state === 'committing') { <b>Saga</b> }`;",
    ].join("\n");
    expect(peopleStrings("probe.ts", source).filter(banned)).toEqual([
      "Compensate the step",
      "Try the step",
      "Rollback",
      "Saga",
    ]);
  });
});
