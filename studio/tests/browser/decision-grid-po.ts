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
// Published decision tables on the mocked platform, and the grid that edits
// them in step details.
import type { Page } from "@playwright/test";

export interface TableDocumentFixture {
  apiVersion: "weave/v1alpha1";
  kind: "DecisionTable";
  metadata: { name: string; version: string };
  spec: Record<string, unknown>;
}

const ref = (name: string) => ({ ref: `/input/${name}` });
const op = (name: string, ...args: unknown[]) => ({ op: { name, args } });
const lit = (value: unknown) => ({ literal: value });

/** payment-policy: an amount and a tier in, approve and a limit out, three rules and Otherwise. */
export function paymentPolicy(version = "1.0.0"): TableDocumentFixture {
  return {
    apiVersion: "weave/v1alpha1",
    kind: "DecisionTable",
    metadata: { name: "payment-policy", version },
    spec: {
      inputSchema: {
        type: "object",
        properties: { amount: { type: "number" }, tier: { type: "string" } },
        required: ["amount"],
      },
      outputSchema: {
        type: "object",
        properties: {
          approve: { type: "boolean" },
          limit: { type: "number" },
        },
        required: ["approve"],
      },
      hitPolicy: "first",
      rules: [
        {
          id: "big-gold",
          when: op(
            "and",
            op("gt", ref("amount"), lit(1000)),
            op("eq", ref("tier"), lit("gold")),
          ),
          output: lit({ approve: true, limit: 5000 }),
        },
        {
          id: "small",
          when: op("lte", ref("amount"), lit(1000)),
          output: lit({ approve: true, limit: 1000 }),
        },
        {
          id: "known",
          when: op(
            "and",
            op("gt", ref("amount"), lit(0)),
            op("exists", ref("tier")),
          ),
          output: lit({ approve: false, limit: 0 }),
        },
      ],
      defaultOutput: lit({ approve: false, limit: 0 }),
    },
  };
}

export interface TableRoutes {
  lists: number;
  exports: string[];
  /** Answers every later list and export with this status (a platform that went away). */
  fail(status: number): void;
}

/** Routes the project's decision tables: the list, and each version's export. */
export async function routeTables(
  page: Page,
  tables: TableDocumentFixture[] = [paymentPolicy()],
  project = "project",
): Promise<TableRoutes> {
  let failing = 0;
  const routes: TableRoutes = {
    lists: 0,
    exports: [],
    fail: (status) => {
      failing = status;
    },
  };
  const rows = tables.map((document, index) => ({
    id: `t${index + 1}`,
    name: document.metadata.name,
    version: document.metadata.version,
    document,
  }));
  const unavailable = { code: "WV-UNAVAILABLE", message: "Unavailable" };
  await page.route(`**/projects/${project}/decision-tables?*`, (route) => {
    routes.lists++;
    if (failing) return route.fulfill({ status: failing, json: unavailable });
    return route.fulfill({
      json: {
        items: rows.map(({ id, name, version }) => ({ id, name, version })),
        next_cursor: null,
      },
    });
  });
  for (const row of rows)
    await page.route(
      `**/projects/${project}/decision-tables/${row.id}/export`,
      (route) => {
        routes.exports.push(`${row.name}@${row.version}`);
        if (failing)
          return route.fulfill({ status: failing, json: unavailable });
        return route.fulfill({
          json: {
            id: row.id,
            name: row.name,
            version: row.version,
            document: row.document,
          },
        });
      },
    );
  return routes;
}
