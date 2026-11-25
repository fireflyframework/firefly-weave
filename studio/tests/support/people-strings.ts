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
// The strings of a TypeScript source that people read, for tests of Studio's
// words: the platform's own values ("compensate") stay in code and aren't
// among them.
import ts from "typescript";

/** The text an inline template shows: its text and its static labels. */
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
 * Literals with a space, or that start with a capital and a lower-case
 * letter, and the text of inline templates. An expression inside a template
 * literal reads as X. Types, object keys and import paths are code.
 */
export function peopleStrings(file: string, source: string): string[] {
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
