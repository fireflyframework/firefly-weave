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
// Checks alert records with the Python models in
// src/firefly_weave/contracts/alerts.py and returns the platform's
// vocabularies, so Studio's lists and fixtures can't drift from the server.
// CI installs the repository environment; without it, callers skip.
import { execFileSync } from "node:child_process";
import { delimiter, resolve } from "node:path";
import {
  python,
  pythonAvailable as pythonFound,
  repository,
} from "./python-path";

export const pythonAvailable = pythonFound({ path: python });

/** Records by model name: "AlertRuleView", "AlertPage", "AlertChannel"… */
export type AlertRecords = Record<string, unknown[]>;

const script = `
import json, sys
from typing import get_args
from pydantic import TypeAdapter
from firefly_weave.contracts import alerts as a
from firefly_weave.contracts.compatibility import CompatibilityReport
from firefly_weave.contracts.maintenance import RetentionPlan, RetentionApplication
from firefly_weave.contracts.operations_summary import OperationsTelemetry
models = {name: getattr(a, name, None) for name in dir(a)}
models.update(CompatibilityReport=CompatibilityReport, RetentionPlan=RetentionPlan,
              RetentionApplication=RetentionApplication, OperationsTelemetry=OperationsTelemetry)
records = json.load(sys.stdin)
for name, items in records.items():
    adapter = TypeAdapter(models[name])
    for item in items:
        adapter.validate_json(json.dumps(item))
print(json.dumps({
    "signals": list(get_args(a.AlertSignal)),
    "units": list(get_args(a.AlertUnit.__value__)),
    "comparators": list(get_args(a.Comparator.__value__)),
    "severities": list(get_args(a.Severity.__value__)),
    "windows": list(get_args(a.WindowSeconds.__value__)),
    "reasons": list(get_args(a.ResolvedReason.__value__)),
    "records": list(get_args(a.RetainedRecord.__value__)),
}))
`;

/** Throws with Python's error when any record breaks its model. */
export function checkAlertsWithPython(
  records: AlertRecords,
): Record<string, (string | number)[]> {
  return JSON.parse(
    execFileSync(python, ["-c", script], {
      cwd: repository,
      encoding: "utf8",
      timeout: 180_000,
      input: JSON.stringify(records),
      env: {
        ...process.env,
        PYTHONPATH: [resolve(repository, "src"), process.env["PYTHONPATH"]]
          .filter(Boolean)
          .join(delimiter),
      },
    }),
  ) as Record<string, (string | number)[]>;
}
