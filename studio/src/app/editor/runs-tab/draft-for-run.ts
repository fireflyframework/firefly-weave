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
// The draft a run opens in from Operate: the newest draft of the run's
// workflow (the highest version, then the latest revision), read only from
// typed drafts and from no more pages of the project's drafts than its
// caller allows.
import { compareVersions } from "../../operate/runs/run-actions";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export interface DraftCandidate {
  id: string;
  revision: number;
  name: string;
  version: string;
}
export interface DraftPage {
  items: unknown[];
  next_cursor: string | null;
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);

/** A draft of the list as Studio can use it; null for anything not typed as one. */
export function draftCandidate(item: unknown): DraftCandidate | null {
  if (!isRecord(item)) return null;
  const { id, revision } = item;
  const document = item["document"] ?? item["definition"];
  const metadata = isRecord(document) ? document["metadata"] : null;
  if (
    typeof id !== "string" ||
    !UUID.test(id) ||
    typeof revision !== "number" ||
    !Number.isInteger(revision) ||
    revision < 1 ||
    !isRecord(metadata) ||
    typeof metadata["name"] !== "string" ||
    typeof metadata["version"] !== "string"
  )
    return null;
  return { id, revision, name: metadata["name"], version: metadata["version"] };
}

/** The highest version of `name`, then its latest revision; null when none. */
export function newestDraft(
  candidates: readonly DraftCandidate[],
  name: string,
): DraftCandidate | null {
  let best: DraftCandidate | null = null;
  for (const draft of candidates) {
    if (draft.name !== name) continue;
    const order = best ? compareVersions(draft.version, best.version) : 1;
    if (order > 0 || (order === 0 && draft.revision > best!.revision))
      best = draft;
  }
  return best;
}

/**
 * Reads the project's drafts a page at a time, at most `maxPages`, for the
 * newest draft of `name`; `complete` says whether every page was read.
 */
export async function findDraft(
  name: string,
  page: (cursor?: string) => Promise<DraftPage>,
  maxPages: number,
): Promise<{ found: DraftCandidate | null; complete: boolean }> {
  const seen: DraftCandidate[] = [];
  let cursor: string | undefined;
  for (let read = 0; read < maxPages; read++) {
    const answer = await page(cursor);
    for (const item of answer.items) {
      const draft = draftCandidate(item);
      if (draft) seen.push(draft);
    }
    if (!answer.next_cursor)
      return { found: newestDraft(seen, name), complete: true };
    cursor = answer.next_cursor;
  }
  return { found: newestDraft(seen, name), complete: false };
}
