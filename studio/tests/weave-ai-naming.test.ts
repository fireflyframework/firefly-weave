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
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

// Program glossary C7: user-facing text says "Weave AI"; code identifiers, API
// paths, capabilities, roles and storage names keep "lumi".
const src = fileURLToPath(new URL("../src", import.meta.url));
const read = (path: string) => readFileSync(join(src, path), "utf8");

function sources(dir = src): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return sources(path);
    return /\.(ts|html|css)$/.test(name) ? [path] : [];
  });
}

describe("Weave AI naming", () => {
  it("never says Lumi in Studio's source", () => {
    const hits = sources().flatMap((file) =>
      readFileSync(file, "utf8")
        .split(/\r?\n/)
        .flatMap((line, index) =>
          /\bLumi\b/.test(line)
            ? [`${relative(src, file)}:${index + 1}: ${line.trim()}`]
            : [],
        ),
    );
    expect(hits).toEqual([]);
  });

  it("names the assistant Weave AI in the shell, panel and settings", () => {
    expect(read("app/app.html")).toMatch(
      /<weave-icon name="assistant" \/><span>Ask Weave AI<\/span>/,
    );
    const panel = read("app/lumi/lumi-panel.ts");
    expect(panel).toContain('heading="Ask Weave AI"');
    expect(panel).toContain('closeLabel="Close Weave AI"');
    expect(panel).toContain('aria-label="Message to Weave AI"');
    const settings = read("app/settings/settings-page.ts");
    expect(settings).toContain("<h2>AI models</h2>");
    expect(settings).not.toContain("AI setup");
  });

  it("keeps the lumi identifiers the API, roles and storage use", () => {
    expect(read("app/lumi/lumi-panel.ts")).toContain('host.can("lumi.manage")');
    const shell = read("app/app.ts");
    expect(shell).toContain('{ value: "lumi_user", label: "Weave AI user" }');
    expect(shell).toContain(
      '{ value: "lumi_manager", label: "Weave AI manager" }',
    );
  });
});
