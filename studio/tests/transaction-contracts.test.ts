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
// Studio's mirrors of a run's transactions, in-doubt incidents with their
// case links, and the summary's transactions section: the read, and the
// examples in tests/contracts/transaction-examples.ts parsed by the platform.
import { execFileSync } from "node:child_process";
import { delimiter, resolve } from "node:path";
import { describe, expect, it } from "vitest";
import {
  readRunTransactions,
  stepName,
} from "../src/app/operate/transactions/transaction-contracts";
import { runViewQuery } from "../src/app/operate/run-contracts";
import * as examples from "./contracts/transaction-examples";
import {
  python,
  pythonAvailable as pythonFound,
  repository,
} from "./python-path";

const pythonAvailable = pythonFound({ path: python });

/** Each record parsed by the platform's model and written back as the platform sends it. */
const script = `
import json, sys
from pydantic import TypeAdapter
from firefly_weave.contracts.operations import IncidentView
from firefly_weave.contracts.operations_summary import ForbiddenSection, TransactionsSection, UnavailableSection
from firefly_weave.contracts.public import Page
from firefly_weave.contracts.run_views import RunSummary, RunTransactionView
models = {
    "IncidentView": IncidentView,
    "RunTransactionPage": Page[RunTransactionView],
    "RunSummary": RunSummary,
    "TransactionsSection": TransactionsSection | ForbiddenSection | UnavailableSection,
}
records = json.load(sys.stdin)
written = {}
for name, items in records.items():
    adapter = TypeAdapter(models[name])
    written[name] = [adapter.dump_python(adapter.validate_json(json.dumps(item)), mode="json") for item in items]
print(json.dumps(written))
`;
/** A run summary query string as the platform's list reads it: only the values it was given. */
const queryScript = `
import json, sys
from starlette.datastructures import QueryParams
from firefly_weave.api.transport import parse_query
from firefly_weave.contracts.run_views import RunSummaryQuery
query = parse_query(QueryParams(sys.stdin.read()), RunSummaryQuery)
print(json.dumps(query.model_dump(mode="json", exclude_unset=True)))
`;
function runPython(code: string, input: string): unknown {
  return JSON.parse(
    execFileSync(python, ["-c", code], {
      cwd: repository,
      encoding: "utf8",
      timeout: 180_000,
      input,
      env: {
        ...process.env,
        PYTHONPATH: [resolve(repository, "src"), process.env["PYTHONPATH"]]
          .filter(Boolean)
          .join(delimiter),
      },
    }),
  );
}
const platformRoundTrip = (records: Record<string, unknown[]>) =>
  runPython(script, JSON.stringify(records)) as Record<string, unknown[]>;

describe("a run's transactions", () => {
  it("reads 50 at a time, and the next page from its cursor", async () => {
    const asked: string[] = [];
    const api = {
      request: <T>(path: string) => {
        asked.push(path);
        return Promise.resolve(examples.transactionPage as T);
      },
    };
    const first = await readRunTransactions(
      api,
      "/v1/environments/e1",
      examples.ids.loopRun,
      null,
    );
    expect(first).toEqual({
      views: examples.transactionPage.items,
      next: examples.transactionPage.next_cursor,
    });
    await readRunTransactions(
      api,
      "/v1/environments/e1",
      examples.ids.loopRun,
      "djI6bW92ZVs0XQ+/=",
    );
    expect(asked).toEqual([
      `/v1/environments/e1/runs/${examples.ids.loopRun}/transactions?limit=50`,
      `/v1/environments/e1/runs/${examples.ids.loopRun}/transactions?limit=50&cursor=djI6bW92ZVs0XQ%2B%2F%3D`,
    ]);
  });

  it("names a step in a sentence without its undo, release or confirm", () => {
    expect(stepName("add#undo")).toBe("add");
    expect(stepName("hold[3]#cancel")).toBe("hold[3]");
    expect(stepName("hold#confirm")).toBe("hold");
    expect(stepName("move")).toBe("move");
    expect(stepName("move[2][1]")).toBe("move[2][1]");
    // A settle deadline's key keeps its suffix: only the three phases are dropped.
    expect(stepName("move#settle")).toBe("move#settle");
  });
});

describe.skipIf(!pythonAvailable)("the platform's transaction answers", () => {
  it("parse with every field Studio reads, and nothing else", () => {
    const records = {
      IncidentView: [
        examples.settleTimeout,
        examples.undoStuck,
        examples.withheldLinks,
        examples.decided,
      ],
      RunTransactionPage: [examples.transactionPage],
      RunSummary: [examples.runInDoubt],
      TransactionsSection: examples.transactionsSections,
    };
    expect(platformRoundTrip(records)).toEqual(records);
  });

  it("read the Runs filter on transactions as Studio sends it, in the platform's order", () => {
    const query = runViewQuery(examples.byTransactionState).slice(1);
    expect(runPython(queryScript, query)).toEqual({
      transaction_state: [
        ...(examples.byTransactionState.transaction_state ?? []),
      ].sort(),
    });
  });
});
