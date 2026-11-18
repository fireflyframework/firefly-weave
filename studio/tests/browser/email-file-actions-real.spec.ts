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
// Email and file steps executed for real: those that send mail or change
// files ask first, those that read run at once, an unclear mail answer is
// never sent again, and files show as files.
import { expect, test, type Page } from "@playwright/test";
import { stringify } from "yaml";
import {
  PRODUCTION,
  bindable,
  connectionSelect,
  openConnected,
} from "./connections-po";
import {
  accepted,
  executeIn,
  output,
  routeStepTests,
  sent,
  succeeded,
  view,
} from "./step-tests-po";
import { yamlFile } from "./step-details-po";

test.use({ viewport: { width: 1440, height: 900 } });

const action = (
  name: string,
  connector: string,
  sideEffect: string,
  outputSchema: Record<string, unknown> = { type: "object" },
) => ({
  apiVersion: "weave/v1alpha1",
  kind: "Action",
  metadata: { name, version: "1.0.0" },
  spec: {
    implementation: { kind: "connector", uses: connector, action: "run" },
    connection: { connector },
    sideEffect,
    timeoutSeconds: 60,
    inputSchema: {
      type: "object",
      properties: { to: { type: "string" }, folder: { type: "string" } },
    },
    outputSchema,
  },
});
const sendInvoice = action(
  "acme.send-invoice",
  "weave-email@1.0.0",
  "non_idempotent",
);
const listInvoices = action(
  "acme.list-invoices",
  "weave-sftp@1.0.0",
  "read_only",
  {
    type: "object",
    properties: { file: { type: "object" } },
  },
);
const workflow = (uses: string) =>
  yamlFile(
    "invoices.yaml",
    stringify({
      apiVersion: "weave/v1alpha1",
      kind: "Workflow",
      metadata: { name: "invoices", version: "1.0.0" },
      spec: {
        inputSchema: { type: "object" },
        outputSchema: { type: "object" },
        steps: [
          {
            id: "send-invoice",
            kind: "action",
            uses,
            with: {
              object: {
                to: { literal: "billing@acme.test" },
                folder: { literal: "/out" },
              },
            },
          },
        ],
      },
    }),
  );

async function open(
  page: Page,
  document: ReturnType<typeof action>,
  connector: string,
  last: unknown,
) {
  const { details } = await openConnected(
    page,
    {
      bindable: {
        [PRODUCTION]: [
          bindable(1, { name: "acme-mail", connector, endpoint_origin: null }),
        ],
      },
      action: document,
    },
    workflow(`${document.metadata.name}@1.0.0`),
  );
  const steps = await routeStepTests(page, {
    starts: [{ status: 202, json: accepted }],
    views: [view(), last],
  });
  await details.open("send-invoice");
  await connectionSelect(details).click();
  await page
    .getByRole("listbox", { name: "Connection options" })
    .getByRole("option")
    .filter({ hasText: /^acme-mail/ })
    .click();
  return { details, steps };
}

test("a new email action asks before it sends in local", async ({ page }) => {
  const { details, steps } = await open(
    page,
    sendInvoice,
    "weave-email@1.0.0",
    succeeded({}),
  );
  await (await executeIn(details, "Production")).click();
  const question = page.getByRole("alertdialog", {
    name: "Execute send-invoice in Production?",
  });
  await expect(question).toContainText(
    "send-invoice changes data in Production. Repeating it repeats the change.",
  );
  expect(steps.posts).toHaveLength(0);
  await question.getByRole("button", { name: "Run it" }).click();
  await sent(steps, 1);
  expect(steps.posts[0].postDataJSON()).toMatchObject({
    uses: "acme.send-invoice@1.0.0",
    input: { to: "billing@acme.test" },
    acknowledged_side_effect: "non_idempotent",
  });
});

test("an unclear mail answer is never resent", async ({ page }) => {
  const { details, steps } = await open(
    page,
    sendInvoice,
    "weave-email@1.0.0",
    view({
      status: "failed",
      // As the platform reports a failed step: WV-STEP-<code> and its generic message.
      error: {
        code: "WV-STEP-EMAIL_ACCEPTANCE_UNKNOWN",
        message: "The step failed: EMAIL_ACCEPTANCE_UNKNOWN.",
      },
      ended_at: "2026-11-17T10:00:05Z",
    }),
  );
  await (await executeIn(details, "Production")).click();
  await page
    .getByRole("alertdialog", { name: "Execute send-invoice in Production?" })
    .getByRole("button", { name: "Run it" })
    .click();
  await expect(output(details)).toContainText(
    "The mail server's answer was unclear. Weave won't resend it; check the mailbox.",
  );
  expect(steps.posts).toHaveLength(1);
});

test("listing files runs at once, and a file shows as a file", async ({
  page,
}) => {
  const { details, steps } = await open(
    page,
    listInvoices,
    "weave-sftp@1.0.0",
    succeeded({
      file: {
        kind: "weave/file",
        id: "8e1f0c4a-1111-4222-8333-444455556666",
        filename: "invoice.pdf",
        contentType: "application/pdf",
        sizeBytes: 122_880,
        sha256: "ab".repeat(32),
      },
    }),
  );
  await (await executeIn(details, "Production")).click();
  await sent(steps, 1);
  await expect(
    page.getByRole("alertdialog", { name: /^Execute send-invoice/ }),
  ).toHaveCount(0);
  await expect(output(details)).toContainText(
    "file · invoice.pdf · 120 KiB · application/pdf",
  );
});
