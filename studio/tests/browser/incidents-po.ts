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
// A platform with transactions in doubt for the Incidents specs: it filters
// incidents.list by status, code, test runs and priority, pages each list
// with its own cursor, and answers incidents.read by ID.
import type { Page } from "@playwright/test";
import type {
  IncidentRecord,
  IncidentView,
} from "../../src/app/operate/incidents/incidents-model";
import { NOW, highPriority, others } from "../transaction-incidents-fixture";
import { connected } from "./support";

export const environment =
  "**/studio/api/api/v1/tenants/tenant/projects/project/environments/development";

export interface IncidentPlatform {
  /** What the platform holds: a test changes it between reads. */
  items: IncidentRecord[];
  /** Each list read's query, in order. */
  queries: URLSearchParams[];
  /** Each incidents.read, by ID, in order. */
  reads: string[];
}

/** Opens nothing: a platform at 10:54 with these incidents. */
export async function incidentPlatform(
  page: Page,
  options: {
    capabilities?: string[];
    items?: IncidentRecord[];
    pageSize?: number;
    /**
     * The incidents it can't show that are of transactions in doubt: it
     * splits by their code, which its answer doesn't say.
     */
    unavailableInDoubt?: string[];
  } = {},
): Promise<IncidentPlatform> {
  await page.clock.install({ time: new Date(NOW) });
  await connected(page, {
    capabilities: options.capabilities ?? [
      "incident.read",
      "incident.resolve",
      "run.read",
    ],
  });
  const size = options.pageSize ?? 50;
  const platform: IncidentPlatform = {
    items: options.items ?? [...highPriority, ...others],
    queries: [],
    reads: [],
  };
  await page.route(`${environment}/incidents?*`, (route) => {
    const query = new URL(route.request().url()).searchParams;
    platform.queries.push(query);
    const statuses = query.getAll("status");
    const priority = query.get("priority");
    const listed = platform.items.filter((item) => {
      const view = item as IncidentView;
      if ("unavailable" in item)
        return (
          !priority ||
          (options.unavailableInDoubt?.includes(item.id)
            ? "high"
            : "normal") === priority
        );
      return (
        (!statuses.length || statuses.includes(view.status)) &&
        (!query.get("code") || view.code === query.get("code")) &&
        (!query.get("workflow") ||
          view.workflow?.name === query.get("workflow")) &&
        (query.get("include_test") === "true" || !view.test) &&
        (!priority || (view.priority ?? "normal") === priority)
      );
    });
    // A cursor belongs to the list it came from, priority included.
    const list = priority ?? "every";
    const [cursorList, offset] = (query.get("cursor") ?? `${list}:0`).split(
      ":",
    );
    if (cursorList !== list)
      return route.fulfill({
        status: 422,
        json: { code: "WV-VALIDATION", message: "Invalid scope-bound cursor" },
      });
    const start = Number(offset);
    const end = start + size;
    return route.fulfill({
      json: {
        items: listed.slice(start, end),
        next_cursor: end < listed.length ? `${list}:${end}` : null,
      },
    });
  });
  await page.route(`${environment}/incidents/*`, (route) => {
    const id = new URL(route.request().url()).pathname.split("/").pop()!;
    platform.reads.push(id);
    const found = platform.items.find((item) => item.id === id);
    return found
      ? route.fulfill({ json: found })
      : route.fulfill({
          status: 404,
          json: { code: "WV-NOT-FOUND", message: "Incident not found" },
        });
  });
  await page.route(`${environment}/runs/*/history?*`, (route) =>
    route.fulfill({
      json: {
        events: ["run_started", "task_scheduled", "incident_opened"].map(
          (type, index) => ({ id: `e${index}`, type, sequence: index + 1 }),
        ),
        next_cursor: null,
      },
    }),
  );
  return platform;
}
