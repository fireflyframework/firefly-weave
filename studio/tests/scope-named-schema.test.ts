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
import { execFileSync } from "node:child_process";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import type { JsonObject } from "../src/app/forms/core/json";
import { referenceScope, type ScopeOptions } from "../src/app/forms/core/scope";
import {
  eventFieldView,
  namedSchemas,
  PLATFORM_EVENT_INPUT,
  PLATFORM_EVENT_SCHEMA,
} from "../src/app/editor/triggers/platform-event";
import { inputSources, sourceRows } from "../src/app/editor/ndv/panes/views";
import { python, pythonAvailable, repository } from "./python-path";

const available = pythonAvailable();
/** The platform's own view, as the Studio host serves it. */
const view = (
  available
    ? JSON.parse(
        execFileSync(
          python,
          [
            "-c",
            "import json; from firefly_weave.compiler.platform_catalog import platform_schema_bundle as b; from firefly_weave.contracts.transaction_resolve import PLATFORM_EVENT_SCHEMA as n; print(json.dumps(b()[n]))",
          ],
          {
            encoding: "utf8",
            env: { ...process.env, PYTHONPATH: join(repository, "src") },
          },
        ),
      )
    : {}
) as JsonObject;

const reacting = (inputSchema: unknown = PLATFORM_EVENT_INPUT) => ({
  spec: {
    inputSchema,
    steps: [
      { id: "key", kind: "transform", value: { ref: "/input/transaction" } },
      { id: "send", kind: "transform", value: { literal: null } },
    ],
  },
});
const entries = (
  options: ScopeOptions,
  definition: unknown = reacting(),
  step = "send",
) =>
  new Map(
    referenceScope(definition, step, "/value", {
      maxEntries: 400,
      ...options,
    }).entries.map((entry) => [entry.ref, entry]),
  );

describe("a schema the input references by name", () => {
  const CUSTOMER = {
    type: "object",
    required: ["name", "home"],
    properties: {
      name: { type: "string" },
      nick: { type: "string" },
      home: { $ref: "#/$defs/address" },
    },
    $defs: {
      address: { type: "object", properties: { city: { type: "string" } } },
    },
  };
  const named: ScopeOptions = {
    namedSchema: (name) => (name === "customer.json" ? CUSTOMER : undefined),
  };

  it("lists its fields, with the named schema's own references", () => {
    const found = entries(named, reacting({ $ref: "customer.json" }));
    expect(found.get("/input/name")).toMatchObject({
      typeLabel: "Text",
      optional: false,
    });
    expect(found.get("/input/nick")).toMatchObject({ optional: true });
    expect(found.get("/input/home/city")?.typeLabel).toBe("Text");
  });

  it("lists them where a field of the input references it", () => {
    const found = entries(
      named,
      reacting({
        type: "object",
        required: ["customer"],
        properties: { customer: { $ref: "customer.json" } },
      }),
    );
    expect(found.get("/input/customer")?.typeLabel).toBe("Group");
    expect(found.get("/input/customer/home/city")?.typeLabel).toBe("Text");
  });

  it("types what an earlier step makes of it", () => {
    const definition = reacting({ $ref: "customer.json" });
    definition.spec.steps[0]!.value = { ref: "/input/home" };
    expect(
      entries(named, definition).get("/steps/key/output/city")?.typeLabel,
    ).toBe("Text");
  });

  it("is followed only with the option, for a name it knows, and never in a circle", () => {
    const listed = (options: ScopeOptions, schema: unknown) =>
      [...entries(options, reacting(schema)).keys()].filter((ref) =>
        ref.startsWith("/input/"),
      );
    const customer = { $ref: "customer.json" };
    expect(listed(named, customer)).toContain("/input/name");
    expect(listed({}, customer)).toEqual([]);
    expect(listed(named, { $ref: "order.json" })).toEqual([]);
    expect(listed({ namedSchema: () => customer }, customer)).toEqual([]);
  });
});

describe.skipIf(!available)("the platform event's fields", () => {
  const resolve = (name: string) => namedSchemas(eventFieldView(view))(name);
  const found = () =>
    entries({ namedSchema: resolve, inputLabel: "Platform event" });

  it("lists the business key as text or empty, always there", () => {
    expect(found().get("/input/transaction/business_key")).toMatchObject({
      typeLabel: "Text or empty",
      optional: false,
      breadcrumb: "Platform event › transaction › business_key",
    });
  });

  it("marks what only some events carry as optional, and lists what a cause holds", () => {
    const all = found();
    expect(all.get("/input/transaction/incident_id")).toMatchObject({
      typeLabel: "Text or empty",
      optional: true,
    });
    expect(all.get("/input/transaction/cause")).toMatchObject({
      typeLabel: "Group or empty",
      optional: true,
    });
    expect(all.get("/input/transaction/cause/code")).toMatchObject({
      typeLabel: "Text",
      optional: true,
    });
    expect(all.get("/input/transaction/state")).toMatchObject({
      typeLabel: "Choice",
      optional: false,
    });
    expect(all.get("/input/transaction/steps/done")?.typeLabel).toBe(
      "List of groups",
    );
    // Several rules about one value don't hide its type.
    expect(all.get("/input/transaction/workflow/name")?.typeLabel).toBe("Text");
    expect(all.get("/input/transaction/cause/step")?.typeLabel).toBe(
      "Text or empty",
    );
  });

  it("keeps rules that contradict each other as they are", () => {
    const strict = {
      type: "object",
      properties: {
        code: { allOf: [{ type: "string" }, { type: "number" }] },
      },
    };
    expect(eventFieldView(strict)).toEqual(strict);
  });

  it("keeps the view as the host served it", () => {
    const before = JSON.stringify(view);
    eventFieldView(view);
    expect(JSON.stringify(view)).toBe(before);
    expect(namedSchemas(view)(PLATFORM_EVENT_SCHEMA)).toBe(view);
  });

  it("is a source of its own in the Input pane, which says what only some events carry", () => {
    const scope = referenceScope(reacting(), "send", "/value", {
      namedSchema: resolve,
      inputLabel: "Platform event",
      maxEntries: 400,
    });
    expect(inputSources(scope).at(-1)).toEqual({
      id: "input",
      label: "Platform event",
      detail: "",
      icon: "signal",
    });
    const rows = new Map(
      sourceRows(scope.entries, "input").map((row) => [row.ref, row]),
    );
    expect(rows.get("/input/transaction/business_key")).toMatchObject({
      label: "business_key",
      breadcrumb: "Platform event › transaction › business_key",
    });
    expect(
      rows.get("/input/transaction/business_key")?.optional,
    ).toBeUndefined();
    expect(rows.get("/input/transaction/incident_id")?.optional).toBe(true);
    // A cause that is there always has its code; the step only sometimes.
    expect(rows.get("/input/transaction/cause")?.optional).toBe(true);
    expect(rows.get("/input/transaction/cause/code")?.optional).toBeUndefined();
    expect(rows.get("/input/transaction/cause/step")?.optional).toBe(true);
    // Any other input keeps its name and its rows.
    expect(entries({}).get("/input")?.breadcrumb).toBe("Workflow input");
    expect(
      inputSources(referenceScope(reacting(), "send", "/value")).at(-1),
    ).toMatchObject({ label: "Workflow input", icon: "source" });
    const form = { type: "object", properties: { note: { type: "string" } } };
    const [row] = sourceRows(
      referenceScope(reacting(form), "send", "/value").entries,
      "input",
    );
    expect(row).toMatchObject({ label: "note", breadcrumb: "Input › note" });
    expect(row?.optional).toBeUndefined();
  });

  it("lists what a builder looks for first, first", () => {
    const rows = sourceRows(
      referenceScope(reacting(), "send", "/value", { namedSchema: resolve })
        .entries,
      "input",
    );
    expect(
      rows.filter((row) => row.depth === 0).map((row) => row.label),
    ).toEqual(["type", "emitted_at", "transaction", "event_id", "version"]);
    expect(
      rows
        .filter((row) => row.ref?.startsWith("/input/transaction/"))
        .filter((row) => row.depth === 1)
        .slice(0, 4)
        .map((row) => row.label),
    ).toEqual(["workflow", "business_key", "state", "cause"]);
  });
});
