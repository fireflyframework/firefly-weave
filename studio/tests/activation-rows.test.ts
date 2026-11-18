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
// The activation dialog's first two steps: whether the platform runs the
// workflow's language features, and one connection row per slot the
// activation needs, prefilled from this computer's dev bindings.
import { describe, expect, it } from "vitest";
import type { BindableItem } from "../src/app/editor/connections/connections-api";
import {
  chosenSummary,
  connectionRows,
  missingFeatures,
  type ConnectionNeeds,
} from "../src/app/editor/activation/activation-rows";

describe("the language features check", () => {
  it("names the first feature the platform doesn't run, in words", () => {
    expect(
      missingFeatures(
        ["ai.agent", "flow.forEach", "flow.transaction"],
        ["ai.agent", "text.concat"],
      ),
    ).toBe(
      "This platform doesn't run loops yet (flow.forEach). Update the platform first.",
    );
    expect(missingFeatures(["text.join"], [])).toBe(
      "This platform doesn't run joined text yet (text.join). Update the platform first.",
    );
  });

  it("names a feature it has no words for by its name", () => {
    expect(missingFeatures(["flow.later"], ["flow.forEach"])).toBe(
      "This platform doesn't run flow.later yet. Update the platform first.",
    );
  });

  it("lets the dialog go on when the platform runs every feature", () => {
    expect(
      missingFeatures(["flow.forEach"], ["flow.forEach", "ai.agent"]),
    ).toBeNull();
    expect(missingFeatures([], [])).toBeNull();
  });
});

describe("connection rows", () => {
  const NOW = new Date("2026-11-18T10:00:00Z");
  const http = (
    revision: string,
    name: string,
    enabled = true,
  ): BindableItem => ({
    revisionId: revision,
    connectionId: `c-${revision}`,
    name,
    connector: "weave-http@2.0.0",
    enabled,
    origin: "https://crm.example",
  });
  const NEEDS: ConnectionNeeds = {
    environment: "local",
    slots: [{ name: "crm", connector: "weave-http@2.0.0", required: true }],
    aiSteps: [],
    agentToolSlots: [],
    bindable: [],
    devBindings: [],
    now: NOW,
  };

  it("prefills from a dev binding that can still be bound, and says when it was tested", () => {
    const rows = connectionRows({
      ...NEEDS,
      bindable: [http("r-1", "crm-dev"), http("r-2", "crm-sandbox")],
      devBindings: [
        {
          slot: "crm",
          binding: {
            connectionId: "c-r-2",
            revisionId: "r-2",
            name: "crm-sandbox",
            tested: { ok: true, at: "2026-11-18T09:58:00Z" },
          },
        },
      ],
    });
    expect(rows).toEqual([
      {
        slot: "crm",
        connector: "weave-http@2.0.0",
        required: true,
        source: "workflow",
        candidates: [
          expect.objectContaining({ revisionId: "r-1" }),
          expect.objectContaining({ revisionId: "r-2" }),
        ],
        chosen: "r-2",
        chosenBy: "dev-binding",
        tested: "Tested 2 min ago",
      },
    ]);
  });

  it("chooses the only connection when the dev binding can't be bound any more", () => {
    const [row] = connectionRows({
      ...NEEDS,
      bindable: [http("r-1", "crm-dev"), http("r-gone", "crm-old", false)],
      devBindings: [
        {
          slot: "crm",
          binding: {
            connectionId: "c-gone",
            revisionId: "r-gone",
            name: "crm-old",
          },
        },
      ],
    });
    expect([
      row.chosen,
      row.chosenBy,
      row.tested,
      row.candidates.length,
    ]).toEqual(["r-1", "only-candidate", undefined, 1]);
  });

  it("leaves the choice to the person when there are several and no dev binding", () => {
    const [row] = connectionRows({
      ...NEEDS,
      bindable: [http("r-1", "a"), http("r-2", "b")],
    });
    expect([row.chosen, row.chosenBy, row.message]).toEqual([
      undefined,
      undefined,
      undefined,
    ]);
  });

  it("says where to create one when there is none for the connector", () => {
    const [row] = connectionRows({
      ...NEEDS,
      bindable: [{ ...http("r-1", "mail"), connector: "weave-email@1.0.0" }],
    });
    expect(row.message).toBe(
      "No connection for weave-http 2.0.0 in local. Create one in Connections.",
    );
    expect(row.candidates).toEqual([]);
  });

  it("says who can bind when the person may not bind connections here", () => {
    const [row] = connectionRows({
      ...NEEDS,
      bindable: "forbidden",
      devBindings: [
        {
          slot: "crm",
          binding: {
            connectionId: "c-r-1",
            revisionId: "r-1",
            name: "crm-dev",
          },
        },
      ],
    });
    expect([row.chosen, row.message]).toEqual([
      undefined,
      "You can't bind connections in local. Ask an administrator for the connection binder role.",
    ]);
  });

  it("lists AI steps' and agent tools' connections after the workflow's, once per slot", () => {
    const ai: BindableItem = {
      ...http("r-ai", "openai"),
      connector: "weave-openai@1.0.0",
    };
    const rows = connectionRows({
      ...NEEDS,
      aiSteps: [
        {
          name: "model",
          connector: "weave-openai@1.0.0",
          required: true,
          step: "summarize",
        },
        {
          name: "crm",
          connector: "weave-http@2.0.0",
          required: false,
          step: "draft",
        },
      ],
      agentToolSlots: [
        {
          name: "lookup-api",
          connector: "weave-http@2.0.0",
          required: false,
          step: "helper",
          tool: "lookup",
        },
      ],
      bindable: [http("r-1", "crm-dev"), ai],
    });
    expect(
      rows.map((row) => [
        row.slot,
        row.source,
        row.required,
        row.step,
        row.tool,
        row.chosen,
      ]),
    ).toEqual([
      ["crm", "workflow", true, undefined, undefined, "r-1"],
      ["model", "ai-step", true, "summarize", undefined, "r-ai"],
      ["lookup-api", "agent-tool", false, "helper", "lookup", "r-1"],
    ]);
  });

  it("counts the rows Studio chose for the person", () => {
    const rows = connectionRows({
      ...NEEDS,
      slots: [
        { name: "a", connector: "weave-http@2.0.0", required: true },
        { name: "b", connector: "weave-http@2.0.0", required: true },
        { name: "c", connector: "weave-http@2.0.0", required: true },
      ],
      bindable: [http("r-1", "only")],
    });
    expect(chosenSummary(rows)).toBe("3 chosen for you · Review");
    expect(
      chosenSummary(connectionRows({ ...NEEDS, bindable: [] })),
    ).toBeNull();
  });
});
