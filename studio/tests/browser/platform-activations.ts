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
// The environment's activations as the platform keeps them: a list paged as
// it pages, and an activation refused unless its If-Match names the revision
// of the workflow's newest one (or is left out when the workflow has none).
import type { Request, Route } from "@playwright/test";

export interface Activated {
  id: string;
  name: string;
  revision: number;
  retired: boolean;
  request: { version_id: string };
}
/** What the platform answers an activation whose If-Match isn't the newest revision. */
export const STALE = {
  status: 412,
  json: {
    status: 412,
    code: "WV-ETAG",
    message: "Revision precondition failed",
  },
};

export class Activations {
  readonly items: Activated[];
  /** The If-Match of each activation asked for; null without one. */
  readonly ifMatch: (string | null)[] = [];
  constructor(...items: Activated[]) {
    this.items = items;
  }
  newest(name: string): number | null {
    const revisions = this.items
      .filter((item) => item.name === name)
      .map((item) => item.revision);
    return revisions.length ? Math.max(...revisions) : null;
  }
  /** One page of the list, in the order the platform keeps: by ID. */
  list(url: URL) {
    const limit = Number(url.searchParams.get("limit") ?? "50");
    const cursor = url.searchParams.get("cursor");
    const sorted = [...this.items].sort((a, b) => a.id.localeCompare(b.id));
    const start = cursor
      ? sorted.findIndex((item) => item.id === cursor) + 1
      : 0;
    const items = sorted.slice(start, start + limit);
    return {
      items,
      next_cursor:
        start + limit < sorted.length ? items[items.length - 1]!.id : null,
    };
  }
  /** Activates `name` as `id` (again, when it is listed), or refuses it. */
  create(request: Request, name: string, id: string) {
    const sent = request.headers()["if-match"] ?? null;
    this.ifMatch.push(sent);
    const newest = this.newest(name);
    if (sent !== (newest === null ? null : `"${newest}"`)) return STALE;
    const made: Activated = {
      id,
      name,
      revision: (newest ?? 0) + 1,
      retired: false,
      request: {
        version_id: String(
          (request.postDataJSON() as Record<string, unknown>)["version_id"],
        ),
      },
    };
    const at = this.items.findIndex((item) => item.id === id);
    if (at < 0) this.items.push(made);
    else this.items[at] = made;
    return { status: 201, json: made };
  }
  /** Answers the list's route. */
  answerList = (route: Route) =>
    route.fulfill({ json: this.list(new URL(route.request().url())) });
}
