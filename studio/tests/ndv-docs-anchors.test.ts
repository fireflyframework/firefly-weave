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
// Every Docs link in step details lands on a heading the step reference has,
// and the Studio guide keeps its step details section.
import "@angular/compiler";
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { DOCS_BASE, docsUrl } from "../src/app/editor/ndv/header";
import {
  EMAIL_CONNECTOR,
  newConnectorRecipe,
  newHttpRecipe,
} from "../src/app/editor/ndv/owned/owned-actions";

/** Heading anchors the way the docs site makes them: lowercase words joined by hyphens. */
const anchors = (markdown: string): Set<string> =>
  new Set(
    [...markdown.matchAll(/^#{2,4} (.+)$/gm)].map((match) =>
      match[1]
        .toLowerCase()
        .replace(/[^\w\s-]/g, "")
        .trim()
        .replace(/\s+/g, "-"),
    ),
  );

describe("Docs links in step details", () => {
  it("point at headings the step reference has", () => {
    const reference = anchors(
      readFileSync("../docs/guides/studio-step-reference.md", "utf8"),
    );
    expect(DOCS_BASE.endsWith("/guides/studio-step-reference/")).toBe(true);
    const kinds = [
      "action",
      "decisionTable",
      "llm",
      "transform",
      "switch",
      "parallel",
      "wait",
      "signal",
      "humanTask",
      "fail",
      "$trigger",
      "$end",
    ];
    const urls = [
      ...kinds.map((kind) => docsUrl(kind, null)),
      docsUrl("action", newHttpRecipe()),
      docsUrl("action", newConnectorRecipe(EMAIL_CONNECTOR, "send")),
      docsUrl("action", newConnectorRecipe("weave-sftp@1.0.0", "list")),
    ];
    for (const url of urls) expect(reference, url).toContain(url.split("#")[1]);
  });
  it("leaves the Studio guide a section about step details", () => {
    expect(anchors(readFileSync("../docs/guides/studio.md", "utf8"))).toContain(
      "configure-a-step-in-step-details",
    );
  });
});
