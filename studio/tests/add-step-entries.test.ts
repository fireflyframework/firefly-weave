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
import { appName, buildCatalog } from "../src/app/editor/add-step/entries";
import {
  catalogInput,
  entryById,
  installed,
  published,
  viewLabels,
} from "./add-step-fixtures";

const sevenTriggers = [
  "Manual form",
  "Webhook",
  "Schedule",
  "Kafka message",
  "Email",
  "App event",
  "Called by a workflow",
];
const fileSteps = [
  "List files",
  "Read a file",
  "Download a file",
  "Write a file",
  "Move a file",
  "Delete a file",
];

describe("the trigger view", () => {
  it("starts an empty workflow with the seven triggers, then Start from a template", () => {
    const catalog = buildCatalog(catalogInput({ hasSteps: false }));
    const view = catalog.views.get("triggers")!;
    expect(view.title).toBe("What triggers this workflow?");
    expect(view.subtitle).toBe(
      "A trigger starts each run. You can add more triggers later.",
    );
    expect(view.sections.map((section) => section.heading)).toEqual([
      "Triggers",
      "Not sure yet?",
    ]);
    expect(viewLabels(catalog, "triggers")).toEqual([
      ...sevenTriggers,
      "Start from a template",
    ]);
    expect(entryById(catalog, "trigger:webhook").disabled).toBeNull();
    expect(entryById(catalog, "trigger:called").disabled).toBe(
      "Needs a platform with sub-workflows (flow.callWorkflow)",
    );
    expect(entryById(catalog, "trigger:webhook").shape).toBe("trigger");
    expect(entryById(catalog, "command:templates").chevron).toBe(true);
  });

  it("allows one input form, one Called by a workflow and six triggers", () => {
    const withSteps = buildCatalog(catalogInput());
    expect(entryById(withSteps, "trigger:manual").disabled).toBe(
      "This workflow already starts from its input form.",
    );
    expect(viewLabels(withSteps, "add-trigger")).toEqual(sevenTriggers);
    const callable = buildCatalog(
      catalogInput({
        triggers: [{ id: "trigger-0000000a", kind: "called" }],
        language: { features: ["flow.callWorkflow"], pending: new Set() },
      }),
    );
    expect(entryById(callable, "trigger:called").disabled).toBe(
      "Other workflows can already call this one.",
    );
    const full = buildCatalog(
      catalogInput({
        triggers: Array.from({ length: 6 }, (_, i) => ({
          id: `trigger-0000000${i}`,
          kind: "webhook" as const,
        })),
      }),
    );
    expect(entryById(full, "trigger:schedule").disabled).toBe(
      "A workflow can have at most 6 triggers.",
    );
    const pending = buildCatalog(
      catalogInput({
        language: {
          features: ["flow.callWorkflow"],
          pending: new Set(["callable"]),
        },
      }),
    );
    expect(entryById(pending, "trigger:called").disabled).toBe(
      "Studio can't make workflows callable yet.",
    );
  });
});

describe("what happens next", () => {
  it("offers the seven categories, in order", () => {
    const catalog = buildCatalog(catalogInput());
    expect(catalog.views.get("next")!.title).toBe("What happens next?");
    expect(viewLabels(catalog, "next")).toEqual([
      "AI",
      "Action in an app",
      "Data transformation",
      "Flow",
      "Wait",
      "Human review",
      "Add another trigger",
    ]);
    expect(entryById(catalog, "category:flow").open).toBe("category:flow");
    expect(entryById(catalog, "add-trigger").open).toBe("add-trigger");
  });

  it("lists each category's steps in order", () => {
    const catalog = buildCatalog(catalogInput());
    expect(viewLabels(catalog, "category:ai")).toEqual(["AI agent", "AI task"]);
    expect(viewLabels(catalog, "category:data")).toEqual([
      "Transform",
      "Text template",
      "Decision table",
    ]);
    expect(viewLabels(catalog, "category:flow")).toEqual([
      "Decision",
      "Parallel",
      "Loop over items",
      "Call a workflow",
      "Stop with error",
    ]);
    expect(viewLabels(catalog, "category:wait")).toEqual([
      "Wait for time",
      "Wait for signal",
    ]);
    expect(viewLabels(catalog, "category:human")).toEqual([
      "Approve or reject",
      "Free text answer",
      "Custom form",
    ]);
  });

  it("keeps entries the target can't run visible, disabled, with the reason", () => {
    const catalog = buildCatalog(catalogInput());
    expect(entryById(catalog, "kind:forEach").disabled).toBe(
      "Needs a platform with loops (flow.forEach)",
    );
    expect(entryById(catalog, "kind:agent").disabled).toBe(
      "Needs a platform with AI agents (ai.agent)",
    );
    expect(entryById(catalog, "preset:text-template").disabled).toBeNull();
    const runs = buildCatalog(
      catalogInput({
        language: {
          features: ["flow.forEach", "ai.agent"],
          pending: new Set(["forEach"]),
        },
      }),
    );
    expect(entryById(runs, "kind:forEach").disabled).toBe(
      "Studio can't add loops yet.",
    );
    expect(entryById(runs, "kind:agent").disabled).toBe(
      "Studio can't add AI agents yet.",
    );
    expect(entryById(runs, "preset:text-template").disabled).toBe(
      "Needs a platform with text templates (text.concat)",
    );
    const unknown = buildCatalog(catalogInput({ language: null }));
    expect(entryById(unknown, "preset:text-template").disabled).toBe(
      "Needs a platform with text templates (text.concat)",
    );
  });

  it("lets plain step kinds be dragged onto a +, and nothing else", () => {
    const catalog = buildCatalog(catalogInput());
    expect(entryById(catalog, "kind:transform").drag).toBe("transform");
    expect(entryById(catalog, "kind:action").drag).toBe("action");
    expect(entryById(catalog, "preset:approve-reject").drag).toBe("humanTask");
    expect(entryById(catalog, "preset:text-template").drag).toBeNull();
    expect(entryById(catalog, "kind:forEach").drag).toBeNull();
    expect(entryById(catalog, "trigger:webhook").drag).toBeNull();
    expect(entryById(catalog, "common:weave-http:call-api").drag).toBeNull();
  });
});

describe("App event", () => {
  it("opens the apps whose events can start a run, each on its own view", () => {
    const catalog = buildCatalog(catalogInput({ hasSteps: false }));
    const appEvent = entryById(catalog, "trigger:provider");
    expect(appEvent).toMatchObject({ open: "app-events", chevron: true });
    expect(catalog.views.get("app-events")!.title).toBe("App event");
    expect(viewLabels(catalog, "app-events")).toEqual([
      "Microsoft Teams",
      "Telegram",
      "WhatsApp",
    ]);
    expect(viewLabels(catalog, "app:weave-telegram")).toContain("App event");
    expect(entryById(catalog, "app:weave-telegram").open).toBe(
      "app:weave-telegram",
    );
  });

  it("stays a plain choice when no app with events is installed", () => {
    const catalog = buildCatalog(
      catalogInput({
        connectors: installed,
        actions: published,
        catalog: "ready",
      }),
    );
    expect(entryById(catalog, "trigger:provider")).toMatchObject({
      open: null,
      chevron: false,
      insert: { kind: "trigger", trigger: "provider" },
    });
  });
});

describe("actions in apps", () => {
  it("lists Call an API and Call an action first, then the apps Studio knows by name, HTTP first, as when connected", () => {
    const catalog = buildCatalog(catalogInput());
    expect(viewLabels(catalog, "category:app")).toEqual([
      "Call an API",
      "Call an action",
      "HTTP APIs",
      "Email",
      "FTP",
      "FTPS",
      "Google Drive",
      "Kafka",
      "Microsoft drive",
      "Microsoft Teams",
      "PostgreSQL",
      "SFTP",
      "Telegram",
      "WhatsApp",
    ]);
    expect(entryById(catalog, "common:weave-http:call-api").insert).toEqual({
      kind: "common",
      common: "call-api",
    });
  });

  it("opens an app on its actions and its triggers", () => {
    const catalog = buildCatalog(catalogInput());
    const email = catalog.views.get("app:weave-email")!;
    expect(email.title).toBe("Email");
    expect(
      email.sections.map((section) => [
        section.heading,
        section.entries.map((entry) => entry.label),
      ]),
    ).toEqual([
      ["Actions (2)", ["Send email", "Reply to an email"]],
      ["Triggers (1)", ["Email"]],
    ]);
    expect(email.note).toBe(
      "Connect to a platform to see its published actions.",
    );
    expect(viewLabels(catalog, "app:weave-sftp")).toEqual(fileSteps);
    expect(entryById(catalog, "common:weave-sftp:write-file")).toMatchObject({
      description: "SFTP · Save a file to a folder",
      insert: {
        kind: "common",
        common: "write-file",
        connector: "weave-sftp@1.0.0",
      },
    });
    expect(appName("weave-email")).toBe("email");
    expect(appName("weave-google-drive")).toBe("Google Drive");
  });

  it("lists the built-in apps, the installed ones and their published action counts, HTTP first", () => {
    const catalog = buildCatalog(
      catalogInput({
        connectors: installed,
        actions: published,
        catalog: "ready",
      }),
    );
    expect(viewLabels(catalog, "category:app")).toEqual([
      "Call an API",
      "Call an action",
      "HTTP APIs",
      "Email",
      "FTP",
      "FTPS",
      "Google Drive",
      "Microsoft drive",
      "PostgreSQL",
      "SFTP",
      "Custom workers",
    ]);
    expect(entryById(catalog, "app:weave-postgresql").meta).toBe("1 action");
    const app = catalog.views.get("app:weave-postgresql")!;
    expect(app.sections[0].heading).toBe("Actions (1)");
    expect(viewLabels(catalog, "app:weave-postgresql")).toEqual([
      "sql.lookup",
      "All versions of sql.lookup",
    ]);
    expect(entryById(catalog, "action:sql.lookup@1.1.0").insert).toEqual({
      kind: "action",
      uses: "sql.lookup@1.1.0",
      id: "a2",
    });
    const versions = catalog.views.get("versions:sql.lookup")!;
    expect(
      versions.sections[0].entries.map((entry) => entry.description),
    ).toEqual(["Version 1.1.0 · read", "Version 1.0.0 · read"]);
    expect(versions.parent).toBe("app:weave-postgresql");
    expect(
      catalog.views.get("workers")!.sections.map((section) => section.heading),
    ).toEqual(["crm-lookup"]);
  });

  it("offers Call an API and New API action under HTTP APIs; New API action needs publishing", () => {
    const catalog = buildCatalog(
      catalogInput({ catalog: "ready", canCreateActions: false }),
    );
    expect(viewLabels(catalog, "app:weave-http")).toEqual([
      "Call an API",
      "New API action",
    ]);
    expect(entryById(catalog, "command:new-api-action").disabled).toBe(
      "Your account can't publish actions in this workspace.",
    );
  });

  it("says when published actions are loading, refused or failed, and offers to try again or load more", () => {
    expect(
      buildCatalog(catalogInput({ catalog: "loading" })).views.get(
        "app:weave-email",
      )!.note,
    ).toBe("Loading published actions…");
    expect(
      buildCatalog(catalogInput({ catalog: "forbidden" })).views.get(
        "app:weave-email",
      )!.note,
    ).toBe("Your account can't read the published actions in this workspace.");
    const failed = buildCatalog(catalogInput({ catalog: "error" }));
    expect(failed.views.get("app:weave-email")!.note).toBe(
      "Published actions couldn't be loaded.",
    );
    expect(viewLabels(failed, "app:weave-email")).toContain("Try again");
    const more = buildCatalog(
      catalogInput({ catalog: "ready", moreActions: true }),
    );
    expect(viewLabels(more, "app:weave-email")).toContain("Load more actions");
  });
});

describe("search's pool", () => {
  it("holds every entry once, without the category rows", () => {
    const catalog = buildCatalog(
      catalogInput({
        connectors: installed,
        actions: published,
        catalog: "ready",
      }),
    );
    const ids = catalog.searchable.map((entry) => entry.id);
    expect(new Set(ids).size).toBe(ids.length);
    for (const id of [
      "trigger:webhook",
      "kind:transform",
      "kind:action",
      "preset:approve-reject",
      "common:weave-http:call-api",
      "common:weave-email:send-email",
      "common:weave-sftp:write-file",
      "app:weave-email",
      "action:sql.lookup@1.1.0",
      "action:sql.lookup@1.0.0",
      "command:new-api-action",
    ])
      expect(ids, id).toContain(id);
    expect(ids).not.toContain("category:flow");
    expect(catalog.customCall).toMatchObject({
      id: "custom-call",
      label: "Make a custom API call",
      insert: { kind: "common", common: "call-api" },
    });
  });
});
