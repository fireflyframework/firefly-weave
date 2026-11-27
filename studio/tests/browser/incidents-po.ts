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
// with its own cursor, and answers incidents.read by ID. An earlier one
// refuses the priority, or ignores it.
import type { Page, Route } from "@playwright/test";
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

/** An incident as a release before priorities answers it. */
export const earlier = ({
  priority: _priority,
  transaction: _transaction,
  waits_on: _waitsOn,
  held_by: _heldBy,
  ...incident
}: IncidentView): IncidentView => incident;

/** Opens nothing: a platform at 10:54 with these incidents. */
export async function incidentPlatform(
  page: Page,
  options: {
    capabilities?: string[];
    items?: IncidentRecord[];
    pageSize?: number;
    /**
     * What it does with `priority`: splits the incidents by it (by default),
     * refuses it as 0.1.0a16 to 0.1.0a19 do, or ignores it as earlier
     * releases do.
     */
    priority?: "splits" | "refuses" | "ignores";
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
    const asked = query.get("priority");
    if (asked && options.priority === "refuses")
      return route.fulfill({
        status: 422,
        json: { code: "WV-VALIDATION", message: "Unknown query parameter" },
      });
    const priority = options.priority === "ignores" ? null : asked;
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

/** A decision Studio sent: the incident, its revision (If-Match) and the body. */
export interface SentDecision {
  id: string;
  revision: string;
  body: Record<string, unknown> & {
    receipt_id: string;
    kind: string;
    reason: string;
    evidence_reference?: string;
    output?: unknown;
  };
}
/**
 * Answers incidents.resolve, recording what was sent: by default as the
 * platform does, with the incident's own row, which says neither when it
 * opened nor its workflow, its run or its links. `answer` may refuse it.
 */
export async function decisions(
  page: Page,
  platform: IncidentPlatform,
  answer?: (route: Route, sent: SentDecision) => unknown,
): Promise<SentDecision[]> {
  const sent: SentDecision[] = [];
  await page.route(`${environment}/incidents/*/resolve`, async (route) => {
    const request = route.request();
    const id = new URL(request.url()).pathname.split("/").at(-2)!;
    const decision: SentDecision = {
      id,
      revision: request.headers()["if-match"] ?? "",
      body: request.postDataJSON() as SentDecision["body"],
    };
    sent.push(decision);
    if (answer && (await answer(route, decision)) !== false) return;
    await resolveIncident(route, platform, decision);
  });
  return sent;
}
/** Resolves an incident on the platform and answers with its row. */
export async function resolveIncident(
  route: Route,
  platform: IncidentPlatform,
  { id, body }: SentDecision,
) {
  const before = platform.items.find((item) => item.id === id) as IncidentView;
  const { waits_on: _waitsOn, held_by: _heldBy, ...kept } = before;
  const after: IncidentView = {
    ...kept,
    status: "resolved",
    revision: before.revision + 1,
    actor_id: "human",
    resolved_at: NOW,
    resolution: {
      receipt_id: body.receipt_id,
      kind: body.kind as NonNullable<IncidentView["resolution"]>["kind"],
      reason: body.reason,
      output: body.output ?? null,
      evidence_reference: body.evidence_reference ?? null,
    },
  };
  platform.items = platform.items.map((item) =>
    item.id === id ? after : item,
  );
  const {
    opened_at: _openedAt,
    updated_at: _updatedAt,
    workflow: _workflow,
    run_status: _runStatus,
    ...row
  } = after;
  await route.fulfill({ json: row });
}
