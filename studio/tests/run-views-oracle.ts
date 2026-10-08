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
// Validates run view pages with the Python models in
// src/firefly_weave/contracts/run_views.py and returns their vocabularies.
// CI installs the repository environment; without it, callers skip.
import { existsSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { delimiter, resolve } from "node:path";

const root = resolve(import.meta.dirname, "../..");
const python = resolve(
  root,
  process.platform === "win32"
    ? ".venv/Scripts/python.exe"
    : ".venv/bin/python",
);
export const pythonAvailable = existsSync(python);
export interface RunViewPages {
  summaries: unknown[];
  steps: unknown[];
  logs: unknown[];
}
const script = `
import json, sys
from typing import get_args
from firefly_weave.contracts import run_views as v
pages = json.load(sys.stdin)
for name, model in (("summaries", v.RunSummaryPage), ("steps", v.StepFactPage), ("logs", v.RunLogPage)):
    for page in pages[name]:
        model.model_validate_json(json.dumps(page))
print(json.dumps({
    "statuses": get_args(v.RunStatus.__value__),
    "terminal": sorted(v.TERMINAL_RUN_STATUSES),
    "origins": get_args(v.RunOrigin.__value__),
    "orders": get_args(v.RunSummaryOrder.__value__),
    "kinds": get_args(v.StepKind.__value__),
    "stepStatuses": get_args(v.StepStatus.__value__),
    "sources": get_args(v.LogSource.__value__),
    "levels": get_args(v.LogLevel.__value__),
}))
`;
/** Throws with Python's error when any page breaks the contract. */
export function validateWithPython(
  pages: RunViewPages,
): Record<string, string[]> {
  return JSON.parse(
    execFileSync(python, ["-c", script], {
      cwd: root,
      input: JSON.stringify(pages),
      encoding: "utf8",
      timeout: 180_000,
      env: {
        ...process.env,
        PYTHONPATH: [resolve(root, "src"), process.env.PYTHONPATH]
          .filter(Boolean)
          .join(delimiter),
      },
    }),
  ) as Record<string, string[]>;
}
