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
// The newest draft of a run's workflow: the highest version, then the
// latest revision, read only from typed drafts and at most five pages.
import { describe, expect, it } from "vitest";
import {
  draftCandidate,
  findDraft,
  newestDraft,
} from "../src/app/editor/runs-tab/draft-for-run";

const id = (n: number) =>
  `${String(n).padStart(8, "0")}-0000-4000-8000-000000000000`;
const item = (
  n: number,
  name: string,
  version: string,
  revision: number,
): Record<string, unknown> => ({
  id: id(n),
  revision,
  document: { metadata: { name, version } },
});

describe("the newest draft of a run's workflow", () => {
  it("picks the highest version, then the latest revision", () => {
    const candidates = [
      item(1, "invoice-approval", "1.2.0", 9),
      item(2, "invoice-approval", "1.3.0", 2),
      item(3, "invoice-approval", "1.3.0", 5),
      item(4, "other", "9.0.0", 1),
    ].map((draft) => draftCandidate(draft)!);
    expect(newestDraft(candidates, "invoice-approval")).toEqual({
      id: id(3),
      revision: 5,
      name: "invoice-approval",
      version: "1.3.0",
    });
    expect(newestDraft(candidates, "nobody")).toBeNull();
  });

  it("reads only typed drafts", () => {
    expect(draftCandidate(item(1, "a", "1.0.0", 1))).not.toBeNull();
    expect(
      draftCandidate({ id: id(1), revision: 1, document: { metadata: {} } }),
    ).toBeNull();
    expect(
      draftCandidate({ ...item(1, "a", "1.0.0", 1), id: "d-1" }),
    ).toBeNull();
    expect(draftCandidate(item(1, "a", "1.0.0", 0))).toBeNull();
    expect(draftCandidate(null)).toBeNull();
    expect(draftCandidate("draft")).toBeNull();
  });

  it("stops after five pages and says it didn't see them all", async () => {
    const asked: (string | undefined)[] = [];
    const page = async (cursor?: string) => {
      asked.push(cursor);
      const n = Number(cursor ?? 0);
      return {
        items: [item(n + 10, "other", "1.0.0", 1)],
        next_cursor: String(n + 1),
      };
    };
    expect(await findDraft("invoice-approval", page)).toEqual({
      found: null,
      complete: false,
    });
    expect(asked).toEqual([undefined, "1", "2", "3", "4"]);
  });

  it("looks through every page it reads, and says when it saw them all", async () => {
    const pages = [
      { items: [item(1, "invoice-approval", "1.2.0", 1)], next_cursor: "a" },
      { items: [item(2, "invoice-approval", "1.3.0", 1)], next_cursor: null },
    ];
    let at = 0;
    const found = await findDraft("invoice-approval", async () => pages[at++]);
    expect(found).toEqual({
      found: {
        id: id(2),
        revision: 1,
        name: "invoice-approval",
        version: "1.3.0",
      },
      complete: true,
    });
  });
});
