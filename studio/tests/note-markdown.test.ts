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
  noteTitle,
  parseNote,
  renderNote,
  safeHref,
} from "../src/app/editor/canvas/note-markdown";

/** Just enough of a DOM to see what the renderer builds. */
class FakeNode {
  children: FakeNode[] = [];
  text = "";
  appendChild(child: FakeNode) {
    this.children.push(child);
    return child;
  }
}
class FakeElement extends FakeNode {
  attributes: Record<string, string> = {};
  constructor(readonly tagName: string) {
    super();
  }
  set href(value: string) {
    this.attributes["href"] = value;
  }
  set target(value: string) {
    this.attributes["target"] = value;
  }
  set rel(value: string) {
    this.attributes["rel"] = value;
  }
  set textContent(value: string) {
    this.children = [Object.assign(new FakeNode(), { text: value })];
  }
  set innerHTML(_value: string) {
    throw Error("The renderer must never set innerHTML.");
  }
  set outerHTML(_value: string) {
    throw Error("The renderer must never set outerHTML.");
  }
  insertAdjacentHTML() {
    throw Error("The renderer must never insert HTML.");
  }
  setAttribute(name: string, value: string) {
    this.attributes[name] = value;
  }
}
const fakeDocument = {
  createElement: (tag: string) => new FakeElement(tag),
  createTextNode: (text: string) => Object.assign(new FakeNode(), { text }),
  createDocumentFragment: () => new FakeNode(),
} as unknown as Document;
const render = (text: string) =>
  renderNote(fakeDocument, parseNote(text)) as unknown as FakeNode;
const elements = (node: FakeNode): FakeElement[] =>
  node.children.flatMap((child) => [
    ...(child instanceof FakeElement ? [child] : []),
    ...elements(child),
  ]);
const textOf = (node: FakeNode): string =>
  node.text + node.children.map(textOf).join("");

describe("the notes' Markdown", () => {
  it("reads headings, paragraphs, lists and code blocks", () => {
    expect(
      parseNote(
        "## Reviewed\nwith **ops** and _finance_\n\n- one\n- `two`\n\n1. first\n2. second\n\n```\nraw <b>\n```",
      ),
    ).toEqual([
      {
        kind: "heading",
        level: 2,
        children: [{ kind: "text", text: "Reviewed" }],
      },
      {
        kind: "paragraph",
        children: [
          { kind: "text", text: "with " },
          { kind: "strong", children: [{ kind: "text", text: "ops" }] },
          { kind: "text", text: " and " },
          { kind: "em", children: [{ kind: "text", text: "finance" }] },
        ],
      },
      {
        kind: "list",
        ordered: false,
        items: [
          [{ kind: "text", text: "one" }],
          [{ kind: "code", text: "two" }],
        ],
      },
      {
        kind: "list",
        ordered: true,
        items: [
          [{ kind: "text", text: "first" }],
          [{ kind: "text", text: "second" }],
        ],
      },
      { kind: "code", text: "raw <b>" },
    ]);
  });

  it("keeps snake_case words and lone stars as text", () => {
    expect(parseNote("use order_id_value * 2")).toEqual([
      {
        kind: "paragraph",
        children: [{ kind: "text", text: "use order_id_value * 2" }],
      },
    ]);
  });

  it("links only to http, https and mailto addresses", () => {
    expect(safeHref("https://weave.example/docs")).toBe(
      "https://weave.example/docs",
    );
    expect(safeHref("mailto:ops@example.com")).toBe("mailto:ops@example.com");
    expect(safeHref("javascript:alert(1)")).toBeNull();
    expect(safeHref(" JavaScript:alert(1)")).toBeNull();
    expect(safeHref("data:text/html,<script>")).toBeNull();
    expect(safeHref("/relative/path")).toBeNull();
    expect(
      parseNote("[docs](https://weave.example) [bad](javascript:alert(1))"),
    ).toEqual([
      {
        kind: "paragraph",
        children: [
          {
            kind: "link",
            href: "https://weave.example",
            children: [{ kind: "text", text: "docs" }],
          },
          { kind: "text", text: " bad)" },
        ],
      },
    ]);
  });

  it("never builds markup, scripts or images from what a note says", () => {
    const fragment = render(
      [
        "<script>alert(1)</script>",
        "![x](https://example.com/x.png)",
        "<img src=x onerror=alert(1)>",
        "[click](javascript:alert(document.cookie))",
        '<a href="javascript:alert(1)">a</a>',
        "&lt;b&gt; **bold** [docs](https://weave.example)",
      ].join("\n"),
    );
    const found = elements(fragment);
    for (const element of found)
      expect([
        "p",
        "h3",
        "h4",
        "h5",
        "strong",
        "em",
        "code",
        "pre",
        "ul",
        "ol",
        "li",
        "a",
      ]).toContain(element.tagName);
    const links = found.filter((element) => element.tagName === "a");
    expect(links.length).toBeGreaterThan(0);
    for (const link of links) {
      expect(link.attributes["href"]).toMatch(/^(https?:|mailto:)/);
      expect(link.attributes["rel"]).toBe("noopener noreferrer");
      expect(link.attributes["target"]).toBe("_blank");
    }
    expect(textOf(fragment)).toContain("<script>alert(1)</script>");
  });

  it("draws headings as h3 to h5, under the page's own headings", () => {
    expect(
      elements(render("# a\n## b\n### c\n#### d")).map(
        (element) => element.tagName,
      ),
    ).toEqual(["h3", "h4", "h5", "h5"]);
  });

  it("titles a note by its first line", () => {
    expect(noteTitle("## Reviewed with **ops**\nmore")).toBe(
      "Reviewed with ops",
    );
    expect(noteTitle("\n\n- [docs](https://weave.example)")).toBe("docs");
    expect(noteTitle("")).toBe("Empty note");
    expect(noteTitle("x".repeat(100))).toBe(`${"x".repeat(59)}…`);
  });

  it("refuses addresses that only look like web or mail ones", () => {
    for (const href of [
      "//evil.example/x",
      "https://ok.example/x y",
      'https://ok.example/"onmouseover="alert(1)',
      "https://ok.example/<script>",
      "java\tscript:alert(1)",
      "vbscript:msgbox(1)",
      "file:///etc/passwd",
      "mailto:",
      "https://",
    ])
      expect(safeHref(href), href).toBeNull();
    expect(safeHref("HTTPS://weave.example")).toBe("HTTPS://weave.example");
  });

  it("keeps a note's text as text, attributes and handlers included", () => {
    const fragment = render(
      '[x](https://weave.example "title") <b onclick="alert(1)">bold</b> `<i>code</i>`',
    );
    const found = elements(fragment);
    expect(found.map((element) => element.tagName).sort()).toEqual([
      "code",
      "p",
    ]);
    for (const element of found)
      expect(
        Object.keys(element.attributes).filter((name) => /^on/i.test(name)),
      ).toEqual([]);
    expect(textOf(fragment)).toContain('<b onclick="alert(1)">bold</b>');
    expect(textOf(fragment)).toContain("<i>code</i>");
  });

  it("reads 4,000 characters of stars quickly", () => {
    const started = performance.now();
    parseNote("*".repeat(4000));
    for (const mark of ["_", "`", "[", "](", "**_", "# ", "- "])
      parseNote(mark.repeat(Math.floor(4000 / mark.length)));
    expect(performance.now() - started).toBeLessThan(200);
  });
});
