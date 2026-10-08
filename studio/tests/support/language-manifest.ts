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
// The language manifest from the repository's Python, for contract tests. The
// manifest is the single list of step kinds; tests skip when the repository
// environment isn't installed (frontend-only contributors), as the other
// Python-backed contract tests do.
import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import { resolve } from "node:path";
import type { LanguageManifest } from "../../src/app/editor/language-manifest";

const root = resolve(import.meta.dirname, "../../..");
const python = resolve(
  root,
  process.platform === "win32"
    ? ".venv/Scripts/python.exe"
    : ".venv/bin/python",
);
/** How Python builds the manifest; the one line to change if its module moves. */
export const MANIFEST_SOURCE =
  "from firefly_weave.contracts.language import language_manifest as manifest";

export function loadLanguageManifest(): LanguageManifest | null {
  if (!existsSync(python)) return null;
  const script = [
    "import json",
    MANIFEST_SOURCE,
    "value = manifest()",
    'data = value.model_dump(mode="json", by_alias=True) if hasattr(value, "model_dump") else value',
    "print(json.dumps(data))",
  ].join("\n");
  return JSON.parse(
    execFileSync(python, ["-c", script], {
      cwd: root,
      encoding: "utf8",
      timeout: 180_000,
    }),
  ) as LanguageManifest;
}
