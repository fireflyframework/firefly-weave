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
import {
  environmentOf,
  listFailure,
  readPages,
  scopeKeyOf,
  type ListHost,
} from "../src/app/operate/operate-list";

const pages: Record<string, { items: number[]; next_cursor: string | null }> = {
  "": { items: [1, 2], next_cursor: "b" },
  b: { items: [3], next_cursor: "c" },
  c: { items: [4], next_cursor: null },
};
function platform() {
  const queries: string[] = [];
  const read = async (query: string) => {
    queries.push(query);
    return pages[new URLSearchParams(query).get("cursor") ?? ""];
  };
  return { queries, read };
}

describe("reading a list in pages", () => {
  it("reads as many pages of 50 as asked for and says where the next one starts", async () => {
    const { queries, read } = platform();
    expect(await readPages(read, 2)).toEqual({ items: [1, 2, 3], cursor: "c" });
    expect(queries).toEqual(["limit=50", "limit=50&cursor=b"]);
  });

  it("stops at the last page", async () => {
    const { queries, read } = platform();
    expect(await readPages(read, 10)).toEqual({
      items: [1, 2, 3, 4],
      cursor: null,
    });
    expect(queries).toHaveLength(3);
  });

  it("reads the page after a cursor for Load more", async () => {
    const { queries, read } = platform();
    expect(await readPages(read, 1, "b")).toEqual({ items: [3], cursor: "c" });
    expect(queries).toEqual(["limit=50&cursor=b"]);
  });

  it("fails when a page fails", async () => {
    const refused = {
      plain: { message: "No", code: "WV-DENIED", status: 403 },
    };
    await expect(readPages(() => Promise.reject(refused), 2)).rejects.toBe(
      refused,
    );
  });
});

describe("a list that could not be read", () => {
  it("shows the access state for a refusal", () => {
    const plain = {
      message: "You don't have permission.",
      code: "WV-DENIED",
      status: 403,
    };
    expect(listFailure({ plain })).toEqual({ error: plain, forbidden: true });
  });

  it("shows the message and support code for anything else", () => {
    const plain = {
      message: "The platform is unavailable.",
      code: "WV-UNAVAILABLE",
      status: 503,
    };
    expect(listFailure({ plain })).toEqual({ error: plain, forbidden: false });
  });
});

describe("what a page reads from", () => {
  const host = (overrides: Partial<ListHost> = {}): ListHost => ({
    profile: { name: "platform" },
    api: { environment: "tenants/t/projects/p/environments/development" },
    identity: { principal_id: "human" },
    signInEnded: false,
    ...overrides,
  });

  it("is the chosen environment, or nothing before one is chosen", () => {
    expect(environmentOf(host())).toBe(
      "tenants/t/projects/p/environments/development",
    );
    expect(environmentOf(host({ profile: null }))).toBe("");
    const unchosen = {
      get environment(): string {
        throw Error("No environment is selected.");
      },
    };
    expect(environmentOf(host({ api: unchosen }))).toBe("");
  });

  it("changes with the environment, the person and an ended sign-in", () => {
    const key = scopeKeyOf(host());
    expect(scopeKeyOf(host())).toBe(key);
    expect(scopeKeyOf(host({ api: { environment: "other" } }))).not.toBe(key);
    expect(scopeKeyOf(host({ identity: { principal_id: "other" } }))).not.toBe(
      key,
    );
    expect(scopeKeyOf(host({ signInEnded: true }))).not.toBe(key);
  });
});
