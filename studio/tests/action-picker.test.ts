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
import "@angular/compiler";
import { describe, expect, it } from "vitest";
import {
  PICKER_LIMIT,
  actionGroup,
  actionPickerItem,
  searchActions,
  type ActionPickerItem,
} from "../src/app/integrations/action-picker";

const http = (name: string, operation: string, description?: string) => ({
  name,
  version: "1.0.0",
  connector: "weave-http@2.0.0",
  operation,
  ...(description ? { description } : {}),
});
const items: ActionPickerItem[] = [
  http("pets.get-pet", "GET /v1/pets/{petId}", "Find one pet"),
  http("crm.lookup-customer", "GET /customers", "Find a customer by email"),
  {
    name: "billing.create-invoice",
    version: "2.0.0",
    worker: "billing.invoice@2.0.0",
  },
  {
    name: "orders.query",
    version: "1.1.0",
    connector: "postgres@1.0.0",
    operation: "query",
  },
  { name: "legacy.thing", version: "0.1.0" },
];

describe("action picker entries", () => {
  it("reads the operation, connector and description from the exported contract", () => {
    const item = actionPickerItem(
      { id: "v1", name: "pets.get-pet", version: "1.0.0" },
      {
        spec: {
          implementation: {
            kind: "connector",
            uses: "weave-http@2.0.0",
            action: "read",
            config: { method: "GET", path: "/v1/pets/{petId}" },
          },
          inputSchema: { description: "Find one pet" },
        },
      },
    );
    expect(item).toEqual({
      id: "v1",
      name: "pets.get-pet",
      version: "1.0.0",
      connector: "weave-http@2.0.0",
      operation: "GET /v1/pets/{petId}",
      description: "Find one pet",
    });
    expect(
      actionPickerItem(
        { name: "billing.create-invoice", version: "2.0.0" },
        {
          spec: {
            implementation: {
              kind: "worker",
              taskType: "billing.invoice",
              taskVersion: "2.0.0",
            },
          },
        },
      ),
    ).toEqual({
      name: "billing.create-invoice",
      version: "2.0.0",
      worker: "billing.invoice@2.0.0",
    });
  });
  it("lists a row before its contract loads and skips retired or unreadable rows", () => {
    expect(actionPickerItem({ name: "a", version: "1.0.0" })).toEqual({
      name: "a",
      version: "1.0.0",
    });
    expect(
      actionPickerItem({ name: "a", version: "1.0.0", retired: true }),
    ).toBeNull();
    expect(actionPickerItem({ id: "x", unavailable: true })).toBeNull();
  });
  it("groups by connector or worker in plain words", () => {
    expect(actionGroup(items[0]).label).toBe("HTTP APIs");
    expect(actionGroup(items[2]).label).toBe("Worker actions");
    expect(actionGroup(items[3]).label).toBe("postgres connector");
    expect(actionGroup(items[4]).label).toBe("Other actions");
  });
});

describe("action search", () => {
  it("finds one action by part of its name", () => {
    const found = searchActions("lookup", items);
    expect(found.count).toBe(1);
    expect(found.groups).toEqual([
      {
        key: "connector:weave-http@2.0.0",
        label: "HTTP APIs",
        items: [items[1]],
      },
    ]);
  });
  it("matches connector, operation and description, ranking names first", () => {
    expect(searchActions("postgres", items).groups[0].items).toEqual([
      items[3],
    ]);
    expect(
      searchActions("customer", items).groups.flatMap((g) => g.items),
    ).toEqual([items[1]]);
    const find = searchActions("find", items).groups.flatMap((g) => g.items);
    expect(find).toEqual([items[0], items[1]]);
    const get = searchActions("get", items).groups.flatMap((g) => g.items);
    // The name match comes before operation matches.
    expect(get[0]).toBe(items[0]);
    expect(get).toContain(items[1]);
    expect(searchActions("pets 1.0.0", items).count).toBe(1);
    expect(searchActions("nothing-like-this", items).count).toBe(0);
  });
  it("lists each name@version once when catalog pages overlap", () => {
    const again = { ...items[1], description: "From the next page" };
    const found = searchActions("", [...items, again]);
    expect(found.count).toBe(items.length);
    const references = found.groups
      .flatMap((g) => g.items)
      .map((i) => `${i.name}@${i.version}`);
    expect(new Set(references).size).toBe(references.length);
  });
  it("keeps every action reachable but renders a bounded list", () => {
    const many = Array.from({ length: PICKER_LIMIT + 5 }, (_, i) => ({
      name: `a${i}`,
      version: "1.0.0",
    }));
    const found = searchActions("", many);
    expect(found.count).toBe(PICKER_LIMIT + 5);
    expect(found.hidden).toBe(5);
    expect(found.groups[0].items).toHaveLength(PICKER_LIMIT);
  });
});

it("shows latest stable semantic versions by name and keeps explicit versions searchable", () => {
  const versions = ["3.0.0", "2.0.0", "3.1.0-rc.1", "3.1.0", "3.10.0", "3.2.0"];
  const actions = versions.map((version) => ({ name: "erp.lookup", version }));
  expect(
    searchActions("erp", actions)
      .groups.flatMap((group) => group.items)
      .map((item) => item.version),
  ).toEqual(["3.10.0"]);
  expect(
    searchActions("erp.lookup@2.0.0", actions).groups[0].items[0].version,
  ).toBe("2.0.0");
});

it("reads worker connection and side-effect metadata for action grouping", () => {
  const item = actionPickerItem(
    { name: "crm.update", version: "1.0.0" },
    {
      spec: {
        implementation: {
          kind: "worker",
          taskType: "crm.update",
          taskVersion: "1.0.0",
        },
        connection: { connector: "salesforce@1.0.0" },
        sideEffect: "non_idempotent",
      },
    },
  );
  expect(item?.connector).toBe("salesforce@1.0.0");
  expect(item?.sideEffect).toBe("non_idempotent");
});
