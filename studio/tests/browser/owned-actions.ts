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
// Publishing a workflow's own actions: a connected platform that keeps its
// catalog in memory and checks with the repository's compiler, and
// order-intake with Send email and Call an API steps whose actions the
// workflow owns.
import { expect, type Page } from "@playwright/test";
import { parse, stringify } from "yaml";
import {
  builtinActions,
  localAuthoring,
  platformCompile,
} from "./local-authoring";
import {
  addCommonStep,
  openStepFixture,
  yamlFile,
  type StepDetailsPage,
} from "./step-details-po";
import { allCapabilities, closeSheet, connected, sourceText } from "./support";

const project = "**/studio/api/api/v1/tenants/tenant/projects/project";

/** order-intake before its steps that own actions are inserted. */
export const ownedWorkflow = `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: order-intake, version: 1.0.0}
spec:
  inputSchema:
    type: object
    properties:
      customerId: {type: string, title: Customer ID}
  outputSchema: {type: object}
  connections:
    crm: {connector: weave-http@2.0.0, required: true}
    mail: {connector: weave-email@1.0.0, required: true}
  steps:
    - id: summary
      kind: transform
      value: {literal: {done: true}}
  output: {literal: {}}
`;

export type Definition = Record<string, unknown> & {
  metadata: { name: string; version: string };
};
export interface PlatformCall {
  path: "actions" | "workflows";
  name: string;
  version: string;
  key: string;
  /** What a workflow publish sent. */
  source?: string;
}
export interface PlatformOptions {
  capabilities?: string[];
  /** Versions already published by someone else. */
  taken?: Definition[];
  /** An action name whose first publish answers 503. */
  failOnce?: string;
  /** The URL typed into Call an API ("" leaves it empty). */
  url?: string;
  /** Holds every action publish until the test releases it. */
  hold?: boolean;
}

/** JSON with sorted keys: two documents with the same content read the same. */
const canonical = (value: unknown): string =>
  JSON.stringify(value, (_key, item: unknown) =>
    item && typeof item === "object" && !Array.isArray(item)
      ? Object.fromEntries(
          Object.entries(item as Record<string, unknown>).sort(([a], [b]) =>
            a < b ? -1 : a > b ? 1 : 0,
          ),
        )
      : item,
  );

/**
 * A connected platform with a catalog in memory. Compiles go to the
 * repository's compiler with what was published; a publish keeps the
 * version, answers the same content with the version it has, and refuses
 * other content under a taken version, as the platform does.
 */
export async function ownedPlatform(page: Page, options: PlatformOptions) {
  const catalog = new Map<string, Definition>(
    (options.taken ?? []).map((definition) => [
      `${definition.metadata.name}@${definition.metadata.version}`,
      definition,
    ]),
  );
  const calls: PlatformCall[] = [];
  let failed = false;
  let release = () => undefined as void;
  const held = options.hold
    ? new Promise<void>((done) => (release = done))
    : null;
  await localAuthoring(page);
  await page.route("**/studio/contracts/connector-actions", (route) =>
    route.fulfill({ json: builtinActions() }),
  );
  await connected(page, {
    capabilities: options.capabilities ?? [...allCapabilities, "compile"],
    catalog: [],
  });
  const compile = (source: unknown, format: string) =>
    platformCompile({ source, format, definitions: [...catalog.values()] });
  await page.route(`${project}/compiler/compile`, async (route) => {
    const body = route.request().postDataJSON() as {
      source: unknown;
      format: string;
    };
    await route.fulfill({ json: await compile(body.source, body.format) });
  });
  await page.route(`${project}/actions`, async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    const definition = parse(
      (route.request().postDataJSON() as { source: string }).source,
    ) as Definition;
    const { name, version } = definition.metadata;
    calls.push({
      path: "actions",
      name,
      version,
      key: route.request().headers()["idempotency-key"] ?? "",
    });
    if (held) await held;
    if (options.failOnce === name && !failed) {
      failed = true;
      return route.fulfill({
        status: 503,
        json: { code: "WV-UNAVAILABLE", message: "The platform is busy." },
      });
    }
    const existing = catalog.get(`${name}@${version}`);
    if (existing && canonical(existing) !== canonical(definition))
      return route.fulfill({
        status: 409,
        json: {
          code: "WV-VERSION-CONFLICT",
          message: "An immutable version already has different content",
        },
      });
    const result = (await compile(definition, "object")) as { ok: boolean };
    if (!result.ok)
      return route.fulfill({
        status: 422,
        json: {
          code: "WV-COMPILE",
          message: "Definition compilation failed",
          result,
        },
      });
    catalog.set(`${name}@${version}`, existing ?? definition);
    await route.fulfill({
      status: 201,
      json: { id: `action-${catalog.size}`, kind: "Action", name, version },
    });
  });
  await page.route(`${project}/workflows`, async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    const body = route.request().postDataJSON() as {
      source: string;
      format: string;
    };
    const { name, version } = (parse(body.source) as Definition).metadata;
    calls.push({
      path: "workflows",
      name,
      version,
      key: route.request().headers()["idempotency-key"] ?? "",
      source: body.source,
    });
    const result = (await compile(body.source, body.format)) as { ok: boolean };
    if (!result.ok)
      return route.fulfill({
        status: 422,
        json: {
          code: "WV-COMPILE",
          message: "Definition compilation failed",
          result,
        },
      });
    await route.fulfill({
      status: 201,
      json: {
        id: "11111111-1111-4111-8111-111111111111",
        kind: "Workflow",
        name,
        version,
        digest: "0".repeat(64),
      },
    });
  });
  return { calls, catalog, release: () => release() };
}

/** Replaces the workflow source in Source and applies it. */
export async function applySource(page: Page, text: string) {
  await closeSheet(page);
  await page.getByRole("tab", { name: "Source", exact: true }).click();
  await page.getByRole("textbox", { name: "Workflow source" }).fill(text);
  await page
    .getByRole("button", { name: "Apply changes", exact: true })
    .click();
  await page.getByRole("tab", { name: "Designer", exact: true }).click();
}

/**
 * order-intake with Send email and Call an API (GET /orders) inserted from
 * the step picker, so the workflow owns both actions; the email's fields
 * come from Source.
 */
export async function withOwnedSteps(
  page: Page,
  options: PlatformOptions = {},
) {
  let platform!: Awaited<ReturnType<typeof ownedPlatform>>;
  const details = await openStepFixture(
    page,
    yamlFile("order-intake.yaml", ownedWorkflow),
    async (p) => {
      platform = await ownedPlatform(p, options);
    },
  );
  await addOwnedSteps(page, details, options);
  return { details, platform };
}
/** Inserts Send email and Call an API from the step picker, and the email's fields from Source. */
export async function addOwnedSteps(
  page: Page,
  details: StepDetailsPage,
  options: PlatformOptions = {},
) {
  await addCommonStep(page, "Send email");
  await expect(details.dialogFor("send-email")).toBeVisible();
  await details.close();
  await addCommonStep(page, "Call an API");
  await expect(details.dialogFor("call-api")).toBeVisible();
  if (options.url !== "") {
    const url = details.field("url").getByRole("textbox", { name: "URL" });
    await url.fill(options.url ?? "/orders");
    await url.press("Tab");
  }
  await details.close();
  const document = parse(await sourceText(page));
  const email = (document.spec.steps as Record<string, unknown>[]).find(
    (step) => step["id"] === "send-email",
  )!;
  email["with"] = {
    literal: {
      to: ["ana@example.com"],
      subject: "Your order",
      text: "Thanks for your order.",
    },
  };
  await applySource(page, stringify(document));
}
export const publishDialog = (page: Page) =>
  page.getByRole("dialog", { name: "Publish order-intake 1.0.0?" });
