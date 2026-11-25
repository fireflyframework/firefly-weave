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
// notify-team, published and about to be activated in an environment whose
// platform event triggers answer as the platform's do: the activation dialog
// lists, creates, moves, replaces and stops them.
import { readFile } from "node:fs/promises";
import {
  expect,
  type Download,
  type Locator,
  type Page,
} from "@playwright/test";
import { stringify } from "yaml";
import { ApiError } from "../../src/app/api";
import { ENV, FakePlatform, type Seed } from "../event-platform";
import { versionId } from "./activation-po";
import { REFERENCE } from "./event-trigger-po";
import { routePlatformEvent } from "./local-authoring";
import { withLanguageFeatures, withNewEditor } from "./step-details-po";
import { allCapabilities, command, connected } from "./support";

export const EVENT_INTENT = "trigger-5f6a7b8c";
export const ATTENTION = {
  events: ["transaction.in_doubt"],
  from: ["move-between-systems"],
};
export const WIDER = {
  events: ["transaction.compensated", "transaction.in_doubt"],
};
/** The trigger an earlier activation made, as this environment stored it. */
export const STORED: Seed = {
  id: "t-1",
  name: "notify-team",
  activation: "a-old",
  revision: 3,
};

const VERSIONS = [
  { id: versionId, name: "notify-team", version: "1.2.0" },
  {
    id: "22222222-2222-4222-8222-222222222222",
    name: "notify-team",
    version: "1.1.0",
  },
  {
    id: "33333333-3333-4333-8333-333333333333",
    name: "notify-team",
    version: "1.0.0",
  },
  {
    id: "44444444-4444-4444-8444-444444444444",
    name: "sync-references",
    version: "2.0.0",
  },
];
const activation = (id: string, name: string, version_id: string) => ({
  id,
  name,
  revision: 1,
  retired: false,
  request: { version_id },
});

export interface EventActivationOptions {
  /** What the version declares; null when it reacts to no platform event. */
  declaration?: Record<string, unknown> | null;
  seeds?: Seed[];
  /** The trigger this environment stored; "t-1" by default, null for none. */
  stored?: string | null;
  pendingStops?: Record<string, unknown>[];
  /** Other trigger choices, before the platform event's. */
  triggers?: Record<string, unknown>[];
  features?: string[];
  /** Whether the activation a-unlisted, past the list's pages, can be read. */
  unlisted?: "readable" | "unreadable";
}
export interface EventActivation {
  platform: FakePlatform;
  /** Requests for the environment's event triggers, in order. */
  asked: { method: string; path: string; ifMatch: string | null }[];
}

/** Answers the environment's event trigger routes from the fake platform. */
export async function routeEventTriggers(
  page: Page,
  platform: FakePlatform,
): Promise<EventActivation["asked"]> {
  const asked: EventActivation["asked"] = [];
  await page.route(
    /\/environments\/development\/event-triggers(?:[/?].*)?$/,
    async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const local =
        url.pathname.slice(
          url.pathname.indexOf("/environments/development") +
            "/environments/development".length,
        ) + url.search;
      const ifMatch = request.headers()["if-match"] ?? null;
      asked.push({ method: request.method(), path: local, ifMatch });
      try {
        const answer =
          request.method() === "GET"
            ? await platform.request(`${ENV}${local}`)
            : await platform.mutate(
                `${ENV}${local}`,
                request.method(),
                request.postDataJSON(),
                ifMatch === null
                  ? undefined
                  : Number(ifMatch.replaceAll('"', "")),
              );
        await route.fulfill({
          status:
            request.method() === "POST" && local === "/event-triggers"
              ? 201
              : 200,
          json: answer,
        });
      } catch (error) {
        // An answer lost on its way: the platform may have done it.
        if (!(error instanceof ApiError)) return route.abort("failed");
        await route.fulfill({
          status: error.status,
          json: { code: error.code, message: error.message },
        });
      }
    },
  );
  return asked;
}

const workflowText = (declaration: Record<string, unknown> | null) =>
  stringify({
    apiVersion: "weave/v1alpha1",
    kind: "Workflow",
    metadata: { name: "notify-team", version: "1.2.0" },
    spec: specOf(declaration),
  });
const specOf = (declaration: Record<string, unknown> | null) => ({
  inputSchema: declaration ? REFERENCE : { type: "object" },
  outputSchema: {},
  ...(declaration ? { eventTrigger: declaration } : {}),
  steps: [{ id: "send", kind: "transform", value: { ref: "/input" } }],
  output: { literal: null },
});

/** Opens notify-team with its trigger choices, connected, and publishes it. */
export async function openForActivation(
  page: Page,
  options: EventActivationOptions = {},
): Promise<EventActivation> {
  const declaration =
    options.declaration === undefined ? ATTENTION : options.declaration;
  const platform = new FakePlatform();
  for (const seed of options.seeds ?? []) platform.seed(seed);
  await withNewEditor(page);
  await routePlatformEvent(page);
  await connected(page, {
    capabilities: allCapabilities,
    environmentName: "development",
  });
  await withLanguageFeatures(page, options.features ?? ["flow.eventTrigger"]);
  const asked = await routeEventTriggers(page, platform);
  const activations = [
    ...(options.unlisted
      ? []
      : [activation("a-older", "notify-team", VERSIONS[2]!.id)]),
    activation("a-old", "notify-team", VERSIONS[1]!.id),
    activation("a-other", "sync-references", VERSIONS[3]!.id),
  ];
  await page.route("**/environments/development/activations?*", (route) =>
    route.fulfill({ json: { items: activations, next_cursor: null } }),
  );
  await page.route("**/environments/development/activations", (route) => {
    if (!activations.some((item) => item.id === "a-new"))
      activations.push(activation("a-new", "notify-team", versionId));
    return route.fulfill({
      status: 201,
      json: activation("a-new", "notify-team", versionId),
    });
  });
  // An old activation, past the pages of the list the dialog reads.
  await page.route(
    "**/environments/development/activations/a-unlisted",
    (route) =>
      options.unlisted === "unreadable"
        ? route.fulfill({
            status: 503,
            json: { code: "WV-UNAVAILABLE", message: "Try again later." },
          })
        : route.fulfill({
            json: activation("a-unlisted", "notify-team", VERSIONS[2]!.id),
          }),
  );
  await page.route(
    `**/projects/project/workflows/${VERSIONS[2]!.id}`,
    (route) => route.fulfill({ json: VERSIONS[2] }),
  );
  await page.route("**/projects/project/workflows?*", (route) =>
    route.fulfill({
      json: {
        items: VERSIONS.filter(
          (item) => !options.unlisted || item.version !== "1.0.0",
        ),
        next_cursor: null,
      },
    }),
  );
  await page.route("**/projects/project/workflows", (route) =>
    route.fulfill({
      status: 201,
      json: { ...VERSIONS[0], digest: "e".repeat(64) },
    }),
  );
  await page.route(
    `**/projects/project/workflows/${versionId}/export`,
    (route) =>
      route.fulfill({
        json: {
          id: versionId,
          document: {
            apiVersion: "weave/v1alpha1",
            kind: "Workflow",
            metadata: { name: "notify-team", version: "1.2.0" },
            spec: specOf(declaration),
          },
          artifact: {
            executable: {
              features: declaration ? ["flow.eventTrigger"] : [],
              connections: {},
              dependencies: [],
            },
          },
        },
      }),
  );
  const stored = options.stored === undefined ? "t-1" : options.stored;
  await openFiles(page, declaration, [
    ...(options.triggers ?? []),
    ...(declaration
      ? [
          {
            id: EVENT_INTENT,
            kind: "event",
            ...(stored && options.seeds?.length
              ? { resources: { development: stored } }
              : {}),
            ...(options.pendingStops
              ? { pendingStops: { development: options.pendingStops } }
              : {}),
          },
        ]
      : []),
  ]);
  return { platform, asked };
}

/** Opens the workflow with these trigger choices and publishes it. */
export async function openFiles(
  page: Page,
  declaration: Record<string, unknown> | null,
  triggers: Record<string, unknown>[],
) {
  await page.getByLabel("Choose a workflow file").setInputFiles([
    {
      name: "notify-team.yaml",
      mimeType: "application/yaml",
      buffer: Buffer.from(workflowText(declaration)),
    },
    {
      name: "notify-team.canvas.json",
      mimeType: "application/json",
      buffer: Buffer.from(
        JSON.stringify({
          schemaVersion: 2,
          kind: "weave.studio/canvas",
          triggers,
        }),
      ),
    },
  ]);
  await expect(page.locator(".editor-bar")).toBeVisible();
  await command(page, "Publish…");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Publish version" })
    .click();
}

export async function activationDialog(page: Page): Promise<Locator> {
  await command(page, "Activate…");
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("Activate notify-team 1.2.0");
  await expect(dialog.getByRole("status")).toHaveCount(0);
  return dialog;
}
export const activate = (dialog: Locator) =>
  dialog.getByRole("button", { name: "Activate version", exact: true }).click();
export const eventRows = (dialog: Locator) =>
  dialog.getByRole("list", { name: "Triggers" }).getByRole("listitem");

/** A trigger choice as the workflow's canvas keeps it, read back through Save to file. */
export async function storedIntent(
  page: Page,
  id = EVENT_INTENT,
): Promise<Record<string, unknown> | null> {
  const downloads: Download[] = [];
  const listener = (download: Download) => downloads.push(download);
  page.on("download", listener);
  await command(page, "Save to file");
  await expect.poll(() => downloads.length).toBe(2);
  page.off("download", listener);
  const file = downloads.find((download) =>
    download.suggestedFilename().endsWith(".canvas.json"),
  )!;
  const saved = JSON.parse(await readFile((await file.path())!, "utf8")) as {
    triggers?: Record<string, unknown>[];
  };
  return saved.triggers?.find((intent) => intent["id"] === id) ?? null;
}
