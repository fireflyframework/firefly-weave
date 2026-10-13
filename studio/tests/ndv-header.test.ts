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
  DOCS_BASE,
  docsUrl,
  headerSubtitle,
  implementationText,
  ownedEntryLabel,
  sideEffectText,
} from "../src/app/editor/ndv/header";
import {
  newConnectorRecipe,
  newHttpRecipe,
} from "../src/app/editor/ndv/owned/owned-actions";
import type { KindContext } from "../src/app/editor/ndv/registry";
import { createStep, freshWorkflow } from "../src/app/model";

const ctx = (owned: Record<string, unknown> = {}): KindContext => ({
  workflow: freshWorkflow(),
  features: [],
  actionContract: () => null,
  tableContract: () => null,
  workflowContract: () => null,
  ownedAction: (uses) => (owned[uses] as never) ?? null,
});

describe("step details header text", () => {
  it("names owned actions by what they do", () => {
    expect(ownedEntryLabel(newHttpRecipe())).toBe("Call an API");
    expect(
      ownedEntryLabel(newConnectorRecipe("weave-email@1.0.0", "send")),
    ).toBe("Send email");
    expect(
      ownedEntryLabel(newConnectorRecipe("weave-email@1.0.0", "reply")),
    ).toBe("Reply to an email");
    expect(
      ownedEntryLabel(newConnectorRecipe("weave-sftp@1.0.0", "list")),
    ).toBe("List files");
    expect(
      ownedEntryLabel(newConnectorRecipe("weave-sftp@1.0.0", "download")),
    ).toBe("Download a file");
    expect(
      ownedEntryLabel(newConnectorRecipe("weave-sftp@1.0.0", "rotate")),
    ).toBe("rotate");
    expect(
      ownedEntryLabel(newConnectorRecipe("weave-sftp@1.0.0", "constructor")),
    ).toBe("constructor");
  });
  it("writes the subtitle under the step name", () => {
    const api = { ...createStep("action", "get"), uses: "flow.get@1.0.0" };
    expect(
      headerSubtitle(
        api,
        ctx({ "flow.get@1.0.0": newHttpRecipe() }),
        "Call an action",
      ),
    ).toBe("Call an API · HTTP request");
    const email = {
      ...createStep("action", "send"),
      uses: "flow.send-email@1.0.0",
      with: { literal: { to: ["a@acme.test", "b@acme.test"] } },
    };
    const sendEmail = ctx({
      "flow.send-email@1.0.0": newConnectorRecipe("weave-email@1.0.0", "send"),
    });
    expect(headerSubtitle(email, sendEmail, "Call an action")).toBe(
      "Send email · 2 recipients",
    );
    expect(
      headerSubtitle(
        { ...email, with: { literal: { to: ["a@acme.test"] } } },
        sendEmail,
        "Call an action",
      ),
    ).toBe("Send email · 1 recipient");
    expect(
      headerSubtitle(
        { ...email, with: { object: { to: { ref: "/input/emails" } } } },
        sendEmail,
        "Call an action",
      ),
    ).toBe("Send email · mapped recipients");
    expect(
      headerSubtitle(
        { ...email, with: { literal: {} } },
        sendEmail,
        "Call an action",
      ),
    ).toBe("Send email · no recipients yet");
    expect(
      headerSubtitle(createStep("switch", "route"), ctx(), "Decision"),
    ).toBe("Decision · the first path that applies runs");
    expect(
      headerSubtitle(createStep("wait", "wait-1"), ctx(), "Wait for time"),
    ).toBe("Wait for time · 1 min");
  });
  it("writes the subtitle of reply and file steps", () => {
    const reply = {
      ...createStep("action", "answer"),
      uses: "flow.reply@1.0.0",
    };
    expect(
      headerSubtitle(
        reply,
        ctx({
          "flow.reply@1.0.0": newConnectorRecipe("weave-email@1.0.0", "reply"),
        }),
        "Call an action",
      ),
    ).toBe("Reply to an email · reply in conversation");
    const list = {
      ...createStep("action", "files"),
      uses: "flow.list@1.0.0",
      with: { literal: { path: "/in" } },
    };
    const listing = ctx({
      "flow.list@1.0.0": newConnectorRecipe("weave-sftp@1.0.0", "list"),
    });
    expect(headerSubtitle(list, listing, "Call an action")).toBe(
      "List files · /in",
    );
    expect(
      headerSubtitle(
        { ...list, with: { literal: {} } },
        listing,
        "Call an action",
      ),
    ).toBe("List files");
  });
  it("describes how an action runs and what it changes", () => {
    expect(
      implementationText({
        spec: {
          implementation: {
            kind: "connector",
            uses: "weave-http@2.0.0",
            action: "read",
            config: { method: "GET", path: "/customers/{customerId}/orders" },
          },
        },
      }),
    ).toBe("weave-http@2.0.0 · GET /customers/{customerId}/orders");
    expect(
      implementationText({
        spec: {
          implementation: {
            kind: "worker",
            taskType: "check-customer",
            taskVersion: "1.0.0",
          },
        },
      }),
    ).toBe("Worker task check-customer 1.0.0");
    expect(
      implementationText({
        spec: {
          implementation: { kind: "connector", uses: "weave-sftp@1.0.0" },
        },
      }),
    ).toBe("weave-sftp@1.0.0");
    expect(implementationText(null)).toBe(
      "Not known until the action's details load",
    );
    expect(sideEffectText("read_only")).toBe("Reads data · safe to retry");
    expect(sideEffectText("non_idempotent")).toBe(
      "Changes data · never retried automatically",
    );
    expect(sideEffectText("")).toBe(
      "Not known until the action's details load",
    );
  });
  it("links each kind to its guide", () => {
    expect(docsUrl("switch", null)).toBe(`${DOCS_BASE}#decision`);
    expect(docsUrl("action", newHttpRecipe())).toBe(`${DOCS_BASE}#call-an-api`);
    expect(
      docsUrl("action", newConnectorRecipe("weave-email@1.0.0", "send")),
    ).toBe(`${DOCS_BASE}#email-and-file-steps`);
    expect(docsUrl("$trigger", null)).toBe(`${DOCS_BASE}#manual-form-trigger`);
    expect(docsUrl("constructor", null)).toBe(
      `${DOCS_BASE}#configure-each-step`,
    );
    expect(docsUrl("somethingNew", null)).toBe(
      `${DOCS_BASE}#configure-each-step`,
    );
  });
});
