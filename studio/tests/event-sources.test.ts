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
import { describe, expect, it } from "vitest";
import type { CatalogLockState } from "../src/app/editor/calls/catalog-lock";
import { sourceSuggestions } from "../src/app/editor/triggers/event-sources";

const ready = (...uses: string[]): CatalogLockState => ({
  kind: "ready",
  lock: {
    workflows: new Map(
      uses.map((item) => {
        const [name, version] = item.split("@") as [string, string];
        return [item, { uses: item, name, version, document: {} }];
      }),
    ),
    schemas: {},
  },
});
const draft = (name: string, version = "1.0.0") => ({
  name,
  version,
  document: null,
});
const PROJECT = ready(
  "move-files-nightly@2.3.0",
  "move-files-nightly@2.10.0",
  "monitor-queues@1.0.4",
  "sync-records@1.0.0",
);
const ask = (options: Partial<Parameters<typeof sourceSuggestions>[0]> = {}) =>
  sourceSuggestions({
    query: "",
    chosen: [],
    own: "notify-team",
    lock: PROJECT,
    drafts: [],
    ...options,
  });

describe("the workflows a trigger can be told to react to", () => {
  it("lists the project's published workflows with their latest version, then the drafts on this computer", () => {
    expect(
      ask({ drafts: [draft("move-archive"), draft("move-archive", "1.1.0")] }),
    ).toEqual({
      note: null,
      groups: [
        {
          label: "In this project",
          items: [
            { name: "monitor-queues", aside: "1.0.4" },
            { name: "move-files-nightly", aside: "2.10.0" },
            { name: "sync-records", aside: "1.0.0" },
          ],
        },
        {
          label: "On this computer",
          items: [{ name: "move-archive", aside: "Draft, not published" }],
        },
      ],
    });
  });

  it("narrows by what is typed, anywhere in the name and in any case", () => {
    const found = ask({ query: "QUEUE", drafts: [draft("move-archive")] });
    expect(found.groups).toEqual([
      {
        label: "In this project",
        items: [{ name: "monitor-queues", aside: "1.0.4" }],
      },
    ]);
  });

  it("leaves out the names already chosen, the workflow's own, and a published workflow's draft", () => {
    const found = ask({
      chosen: ["sync-records"],
      own: "monitor-queues",
      drafts: [
        draft("move-files-nightly"),
        draft("monitor-queues"),
        draft("sync-records"),
      ],
    });
    expect(found.groups).toEqual([
      {
        label: "In this project",
        items: [{ name: "move-files-nightly", aside: "2.10.0" }],
      },
    ]);
  });

  it.each<[string, CatalogLockState, string | null]>([
    [
      "no permission",
      { kind: "forbidden" },
      "You can't read this project's published workflows here.",
    ],
    [
      "a read that failed",
      { kind: "failed", message: "The platform didn't answer." },
      "Studio couldn't read this project's published workflows.",
    ],
    [
      "a read under way",
      { kind: "loading" },
      "Reading this project's published workflows…",
    ],
    ["no platform", { kind: "offline" }, null],
  ])("offers this computer's drafts with %s", (_name, lock, note) => {
    expect(ask({ lock, drafts: [draft("move-archive")] })).toEqual({
      note,
      groups: [
        {
          label: "On this computer",
          items: [{ name: "move-archive", aside: "Draft, not published" }],
        },
      ],
    });
  });

  it("skips drafts without a name a trigger can use", () => {
    expect(
      ask({ lock: { kind: "offline" }, drafts: [draft(""), draft("-x")] })
        .groups,
    ).toEqual([]);
  });
});
