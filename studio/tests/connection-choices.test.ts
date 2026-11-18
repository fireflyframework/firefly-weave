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
  CREATE_CONNECTION,
  RECENT_TEST_MS,
  connectionOptions,
  fieldHint,
  needsTest,
  notEncrypted,
  preselection,
  slotFor,
  staleBinding,
  testPill,
} from "../src/app/editor/connections/connection-choices";
import type { BindableItem } from "../src/app/editor/connections/connections-api";
import type { DevBinding } from "../src/app/editor/connections/dev-bindings";
import type { Workflow } from "../src/app/model";

const id = (n: number) =>
  `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const item = (n: number, over: Partial<BindableItem> = {}): BindableItem => ({
  connectionId: id(n),
  revisionId: id(100 + n),
  name: `acme-${n}`,
  connector: "weave-http@2.0.0",
  enabled: true,
  origin: `https://api${n}.acme.test`,
  ...over,
});
const binding = (n: number, over: Partial<DevBinding> = {}): DevBinding => ({
  connectionId: id(n),
  revisionId: id(100 + n),
  name: `acme-${n}`,
  ...over,
});
const workflow = (connections: Record<string, unknown>): Workflow =>
  ({
    apiVersion: "weave/v1alpha1",
    kind: "Workflow",
    metadata: { name: "order-intake", version: "1.0.0" },
    spec: { connections, steps: [] },
  }) as unknown as Workflow;
const HTTP = { manage: true, connector: "weave-http@2.0.0" };

describe("what the Connection field offers", () => {
  it("lists connections in the environment's group with their addresses", () => {
    const options = connectionOptions(
      [item(1), item(2, { origin: null })],
      "local",
      HTTP,
    );
    expect(options.slice(0, 2)).toEqual([
      {
        value: id(1),
        label: "acme-1",
        description: "https://api1.acme.test",
        group: "In local",
      },
      {
        value: id(2),
        label: "acme-2",
        description: "No address",
        group: "In local",
      },
    ]);
    expect(options.at(-1)).toEqual({
      value: CREATE_CONNECTION,
      label: "Create connection",
      action: true,
    });
  });

  it("a plain-HTTP address is marked as not encrypted", () => {
    const [plain, secure] = connectionOptions(
      [item(1, { origin: "http://legacy.acme.test" }), item(2)],
      "local",
      HTTP,
    );
    expect(plain).toMatchObject({
      description: "http://legacy.acme.test",
      note: "Not encrypted",
    });
    expect(secure.note).toBeUndefined();
  });

  it("a turned-off connection is listed but can't be chosen", () => {
    const [option] = connectionOptions(
      [item(1, { enabled: false })],
      "local",
      HTTP,
    );
    expect(option).toEqual({
      value: id(1),
      label: "acme-1",
      description: "Turned off in local",
      group: "In local",
      disabled: true,
    });
  });

  it("Create connection needs connection management, and only HTTP connections are created here", () => {
    const create = (can: { manage: boolean; connector: string }) =>
      connectionOptions([item(1)], "staging", can).at(-1);
    expect(create(HTTP)).toEqual({
      value: CREATE_CONNECTION,
      label: "Create connection",
      action: true,
    });
    expect(create({ manage: false, connector: "weave-http@2.0.0" })).toEqual({
      value: CREATE_CONNECTION,
      label: "Create connection",
      action: true,
      description:
        "Your account can't manage connections in staging. Ask an administrator.",
      disabled: true,
    });
    expect(create({ manage: true, connector: "weave-sftp@1.0.0" })).toEqual({
      value: CREATE_CONNECTION,
      label: "Create connection",
      action: true,
      description:
        "Studio creates weave-http connections here. Create weave-sftp connections with weave platform integrations, or ask an administrator.",
      disabled: true,
    });
  });

  it("an empty list says so and still offers Create connection", () => {
    const options = connectionOptions([], "local", HTTP);
    expect(options).toHaveLength(2);
    expect(options[0]).toMatchObject({
      label: "No weave-http connections you can use in local.",
      disabled: true,
    });
    expect(options[1]).toEqual({
      value: CREATE_CONNECTION,
      label: "Create connection",
      action: true,
    });
  });

  it("preselects the slot's connection, else the only one, else none", () => {
    expect(preselection([item(1)], null)).toEqual(item(1));
    expect(preselection([item(1), item(2)], null)).toBeNull();
    expect(preselection([item(1), item(2)], binding(2))).toEqual(item(2));
    expect(
      preselection([item(1), item(2, { enabled: false })], binding(2)),
    ).toEqual(item(1));
    expect(preselection([item(1), item(2)], binding(3))).toBeNull();
    expect(preselection([], null)).toBeNull();
  });

  it("uses the workflow's only slot for the connector, else declares a new one", () => {
    const http = { connector: "weave-http@2.0.0" };
    expect(slotFor(workflow({}), "weave-http@2.0.0")).toEqual({
      slot: "http",
      declare: true,
    });
    expect(
      slotFor(
        workflow({ crm: http, mail: { connector: "weave-email@1.0.0" } }),
        "weave-http@2.0.0",
      ),
    ).toEqual({ slot: "crm", declare: false });
    expect(slotFor(workflow({ http, erp: http }), "weave-http@2.0.0")).toEqual({
      slot: "http-2",
      declare: true,
    });
    expect(
      slotFor(workflow({ http, erp: http }), "weave-http@2.0.0", "erp"),
    ).toEqual({ slot: "erp", declare: false });
    expect(
      slotFor(
        workflow({ http, mail: { connector: "weave-email@1.0.0" } }),
        "weave-http@2.0.0",
        "mail",
      ),
    ).toEqual({ slot: "http", declare: false });
  });

  it("the test pill says when and how it went", () => {
    const now = new Date("2026-11-17T10:10:00Z");
    expect(testPill(undefined, now)).toBeNull();
    expect(testPill({ ok: true, at: "2026-11-17T10:09:30Z" }, now)).toEqual({
      text: "Tested just now",
      tone: "success",
      retry: false,
    });
    expect(testPill({ ok: true, at: "2026-11-17T10:08:00Z" }, now)).toEqual({
      text: "Tested 2 min ago",
      tone: "success",
      retry: false,
    });
    expect(testPill({ ok: false, at: "2026-11-17T10:09:59Z" }, now)).toEqual({
      text: "Test failed",
      tone: "danger",
      retry: true,
    });
  });

  it("tests again only after ten minutes, and only for people who manage connections", () => {
    const now = new Date("2026-11-17T10:10:00Z");
    expect(RECENT_TEST_MS).toBe(600_000);
    expect(needsTest(binding(1), true, now)).toBe(true);
    expect(needsTest(binding(1), false, now)).toBe(false);
    expect(needsTest(null, true, now)).toBe(false);
    const at = (iso: string, ok = true) =>
      binding(1, { tested: { ok, at: iso } });
    expect(needsTest(at("2026-11-17T10:01:00Z"), true, now)).toBe(false);
    expect(needsTest(at("2026-11-17T09:59:59Z"), true, now)).toBe(true);
    expect(needsTest(at("2026-11-17T10:09:00Z", false), true, now)).toBe(false);
  });

  it("the hint names the environment, the slot and the address", () => {
    expect(fieldHint("local", "crm", "https://api.acme.test")).toBe(
      "In local · slot crm · https://api.acme.test",
    );
    expect(fieldHint("local", "crm", null)).toBe("In local · slot crm");
  });

  it("a plain-HTTP address is marked not encrypted", () => {
    expect(notEncrypted("http://api.acme.test")).toBe(true);
    expect(notEncrypted("https://api.acme.test")).toBe(false);
    expect(notEncrypted(null)).toBe(false);
  });

  it("a binding to a connection no longer listed says so", () => {
    expect(staleBinding(binding(1), [item(1)], "local")).toBeNull();
    expect(staleBinding(null, [item(1)], "local")).toBeNull();
    expect(staleBinding(binding(3), [item(1)], "local")).toBe(
      "acme-3 isn't available in local anymore. Choose another connection.",
    );
    expect(
      staleBinding(binding(1), [item(1, { enabled: false })], "local"),
    ).toBe("acme-1 is turned off in local. Choose another connection.");
  });
});
