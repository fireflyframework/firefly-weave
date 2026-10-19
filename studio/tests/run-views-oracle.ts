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
const pythonOptions = {
  cwd: root,
  encoding: "utf8" as const,
  timeout: 180_000,
  env: {
    ...process.env,
    PYTHONPATH: [resolve(root, "src"), process.env.PYTHONPATH]
      .filter(Boolean)
      .join(delimiter),
  },
};
/** Throws with Python's error when any page breaks the contract. */
export function validateWithPython(
  pages: RunViewPages,
): Record<string, string[]> {
  return JSON.parse(
    execFileSync(python, ["-c", script], {
      ...pythonOptions,
      input: JSON.stringify(pages),
    }),
  ) as Record<string, string[]>;
}

/** The query models that parse_query decodes in the run views. */
export type QueryModel =
  | "RunSummaryQuery"
  | "RunListQuery"
  | "RunStepQuery"
  | "RunLogQuery";
const queryScript = `
import json, sys
from starlette.datastructures import QueryParams
from firefly_weave.api.transport import parse_query
from firefly_weave.contracts import run_views as v
from firefly_weave.definitions.models import CatalogError
model = getattr(v, sys.argv[1])
def verdict(raw):
    try:
        parse_query(QueryParams(raw), model)
        return "ok"
    except CatalogError as error:
        return error.code
    except ValueError:
        return "WV-VALIDATION"
    except Exception as error:
        return "ERROR:" + type(error).__name__
print(json.dumps([verdict(raw) for raw in json.load(sys.stdin)]))
`;
/**
 * parse_query's verdict for each raw query string, in order: "ok", the
 * CatalogError code, or "WV-VALIDATION". A server crash reads "ERROR:...".
 */
export function classifyQueriesWithPython(
  queries: string[],
  model: QueryModel = "RunSummaryQuery",
): string[] {
  return JSON.parse(
    execFileSync(python, ["-c", queryScript, model], {
      ...pythonOptions,
      input: JSON.stringify(queries),
    }),
  ) as string[];
}

const messageScript = `
import json, sys
from starlette.datastructures import QueryParams
from firefly_weave.api.transport import parse_query
from firefly_weave.contracts.run_views import RunSummaryQuery
from firefly_weave.definitions.models import CatalogError
def message(raw):
    try:
        parse_query(QueryParams(raw), RunSummaryQuery)
        return ""
    except CatalogError as error:
        return error.message
    except ValueError:
        return ""
print(json.dumps([message(raw) for raw in json.load(sys.stdin)]))
`;
/**
 * The message of the CatalogError parse_query raises for each run summary
 * query string, in order; empty when it raises none.
 */
export function rejectionMessagesWithPython(queries: string[]): string[] {
  return JSON.parse(
    execFileSync(python, ["-c", messageScript], {
      ...pythonOptions,
      input: JSON.stringify(queries),
    }),
  ) as string[];
}

/** Serialize fixture pages through Python to check additive wire fields. */
export function roundTripWithPython(pages: RunViewPages): {
  summaries: import("../src/app/operate/run-contracts").RunSummaryPage[];
  steps: import("../src/app/operate/run-contracts").StepFactPage[];
  logs: import("../src/app/operate/run-contracts").RunLogPage[];
} {
  return JSON.parse(
    execFileSync(
      python,
      [
        "-c",
        `
import json, sys
from firefly_weave.contracts import run_views as v
pages = json.load(sys.stdin)
for name, model in (("summaries", v.RunSummaryPage), ("steps", v.StepFactPage), ("logs", v.RunLogPage)):
    pages[name] = [model.model_validate_json(json.dumps(page)).model_dump(mode="json") for page in pages[name]]
print(json.dumps(pages))
`,
      ],
      { ...pythonOptions, input: JSON.stringify(pages) },
    ),
  );
}
