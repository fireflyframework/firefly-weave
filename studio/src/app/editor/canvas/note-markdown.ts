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
// Sticky notes speak a small part of Markdown: headings, bold, italic,
// lists, inline code, code blocks and links to http, https and mailto
// addresses. Everything else stays text. The renderer builds DOM nodes one
// by one, so a note can never inject markup, scripts or images.
export type Inline =
  | { kind: "text"; text: string }
  | { kind: "strong"; children: Inline[] }
  | { kind: "em"; children: Inline[] }
  | { kind: "code"; text: string }
  | { kind: "link"; href: string; children: Inline[] };
export type Block =
  | { kind: "heading"; level: 1 | 2 | 3; children: Inline[] }
  | { kind: "paragraph"; children: Inline[] }
  | { kind: "list"; ordered: boolean; items: Inline[][] }
  | { kind: "code"; text: string };

const FENCE = /^\s{0,3}```/;
const HEADING = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
const BULLET = /^\s{0,3}[-*+]\s+(.*)$/;
const NUMBERED = /^\s{0,3}\d{1,9}[.)]\s+(.*)$/;
/** A web address with a host, or a mail address with a recipient. */
const SAFE = /^(https?:\/\/[^\s/?#\\]+|mailto:[^\s]+)/i;
const STRONG = /^(\*\*|__)(?=\S)([\s\S]*?\S)\1/;
const EM = /^(\*|_)(?=\S)([\s\S]*?\S)\1(?!\1)/;
/** A link's address may hold one level of parentheses, as Wikipedia's do. */
const LINK = /^\[([^\]]+)\]\(((?:[^()\s]|\([^()\s]*\))+)\)/;
const WORD = /[A-Za-z0-9]/;

/** The address when it is http, https or mailto; null for anything else. */
export function safeHref(href: string): string | null {
  const trimmed = href.trim();
  return trimmed === href && SAFE.test(href) && !/[\s<>"]/.test(href)
    ? href
    : null;
}

export function parseInline(text: string): Inline[] {
  const nodes: Inline[] = [];
  let plain = "";
  const push = (node: Inline) => {
    if (plain) nodes.push({ kind: "text", text: plain });
    plain = "";
    nodes.push(node);
  };
  for (let i = 0; i < text.length; ) {
    const rest = text.slice(i);
    if (rest[0] === "`") {
      const end = text.indexOf("`", i + 1);
      if (end > i + 1) {
        push({ kind: "code", text: text.slice(i + 1, end) });
        i = end + 1;
        continue;
      }
    }
    // An underscore inside a word (order_id) is a letter, not emphasis.
    const inWord = rest[0] === "_" && i > 0 && WORD.test(text[i - 1]);
    const strong = inWord ? null : STRONG.exec(rest);
    if (strong) {
      push({ kind: "strong", children: parseInline(strong[2]) });
      i += strong[0].length;
      continue;
    }
    const em = inWord ? null : EM.exec(rest);
    if (em) {
      push({ kind: "em", children: parseInline(em[2]) });
      i += em[0].length;
      continue;
    }
    const link = LINK.exec(rest);
    if (link) {
      const href = safeHref(link[2]);
      if (href) push({ kind: "link", href, children: parseInline(link[1]) });
      else plain += link[1];
      i += link[0].length;
      continue;
    }
    plain += text[i];
    i++;
  }
  if (plain) nodes.push({ kind: "text", text: plain });
  return nodes;
}

export function parseNote(text: string): Block[] {
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  const blocks: Block[] = [];
  let paragraph: string[] = [];
  const flush = () => {
    if (paragraph.length)
      blocks.push({
        kind: "paragraph",
        children: parseInline(paragraph.join(" ")),
      });
    paragraph = [];
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (FENCE.test(line)) {
      flush();
      const code: string[] = [];
      for (i++; i < lines.length && !FENCE.test(lines[i]); i++)
        code.push(lines[i]);
      blocks.push({ kind: "code", text: code.join("\n") });
      continue;
    }
    if (!line.trim()) {
      flush();
      continue;
    }
    const heading = HEADING.exec(line);
    if (heading) {
      flush();
      blocks.push({
        kind: "heading",
        level: Math.min(heading[1].length, 3) as 1 | 2 | 3,
        children: parseInline(heading[2]),
      });
      continue;
    }
    const ordered = !BULLET.test(line) && NUMBERED.test(line);
    if (BULLET.test(line) || ordered) {
      flush();
      const items: Inline[][] = [];
      for (; i < lines.length; i++) {
        const item = (ordered ? NUMBERED : BULLET).exec(lines[i]);
        if (!item) break;
        items.push(parseInline(item[1]));
      }
      i--;
      blocks.push({ kind: "list", ordered, items });
      continue;
    }
    paragraph.push(line.trim());
  }
  flush();
  return blocks;
}

export function renderNote(
  doc: Document,
  blocks: readonly Block[],
): DocumentFragment {
  const fragment = doc.createDocumentFragment();
  const inline = (parent: Node, nodes: readonly Inline[]) => {
    for (const node of nodes) {
      if (node.kind === "text")
        parent.appendChild(doc.createTextNode(node.text));
      else if (node.kind === "code") {
        const code = doc.createElement("code");
        code.textContent = node.text;
        parent.appendChild(code);
      } else if (node.kind === "link") {
        const link = doc.createElement("a");
        link.href = node.href;
        // A mail address opens the mail app; a new tab would stay blank.
        if (!/^mailto:/i.test(node.href)) link.target = "_blank";
        link.rel = "noopener noreferrer";
        inline(link, node.children);
        parent.appendChild(link);
      } else {
        const element = doc.createElement(node.kind);
        inline(element, node.children);
        parent.appendChild(element);
      }
    }
  };
  for (const block of blocks) {
    if (block.kind === "code") {
      const pre = doc.createElement("pre");
      const code = doc.createElement("code");
      code.textContent = block.text;
      pre.appendChild(code);
      fragment.appendChild(pre);
    } else if (block.kind === "list") {
      const list = doc.createElement(block.ordered ? "ol" : "ul");
      for (const item of block.items) {
        const entry = doc.createElement("li");
        inline(entry, item);
        list.appendChild(entry);
      }
      fragment.appendChild(list);
    } else {
      const element = doc.createElement(
        block.kind === "heading" ? `h${block.level + 2}` : "p",
      );
      inline(element, block.children);
      fragment.appendChild(element);
    }
  }
  return fragment;
}

/** A note's name: its first line without Markdown, at most 60 characters. */
export function noteTitle(text: string): string {
  const line = text.split(/\r?\n/).find((item) => item.trim()) ?? "";
  const plain = line
    .replace(/^\s*(#{1,6}\s+|[-*+]\s+|\d+[.)]\s+)/, "")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/[*_`]/g, "")
    .trim();
  if (!plain) return "Empty note";
  return plain.length > 60 ? `${plain.slice(0, 59)}…` : plain;
}
