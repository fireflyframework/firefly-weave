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
import { selectChoice } from "./support";
import { expect, test } from "@playwright/test";
import { createHash } from "node:crypto";
import { parse } from "yaml";
import { DesignerPage } from "./designer-po";
import {
  allCapabilities,
  connected,
  lookupAction,
  newWorkflow,
  offline,
  sourceText,
} from "./support";
import { chooseAction } from "./integrations-po";
import { expectInViewport } from "./ux-assertions";

const fileSchema = {
  type: "object",
  additionalProperties: false,
  required: ["kind", "id", "filename", "contentType", "sizeBytes", "sha256"],
  properties: {
    kind: { const: "weave/file", type: "string" },
    id: { type: "string", format: "uuid" },
    filename: { type: "string", minLength: 1, maxLength: 255 },
    contentType: { type: "string" },
    sizeBytes: { type: "integer", minimum: 0, maximum: 26214400 },
    sha256: { type: "string", pattern: "^[0-9a-f]{64}$" },
  },
};
for (const viewport of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
]) {
  test.describe(`${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport });
    test("File form fields use the canonical host schema and an inert reviewer preview", async ({
      page,
    }) => {
      await offline(page);
      await page.route("**/studio/contracts/file-reference", (route) =>
        route.fulfill({ json: fileSchema }),
      );
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.append("humanTask");
      await designer.selectStep("approval-1");
      const form = designer.inspector.locator('[data-field="formSchema"]');
      await form
        .getByRole("button", { name: "Add field", exact: true })
        .click();
      await form.getByLabel("Field name", { exact: true }).fill("invoice");
      await selectChoice(
        form.getByLabel("Type of “invoice”", { exact: true }),
        "file",
      );
      await expect(form.locator(".sd-preview")).toContainText(
        "File upload preview",
      );
      await expect(form.locator("input[type=file]")).toHaveCount(0);
      const schema = parse(await sourceText(page)).spec.steps[0].formSchema
        .properties.invoice;
      expect(schema).toEqual(fileSchema);
    });
    test("file upload is chunked and source holds only the verified reference", async ({
      page,
    }) => {
      const action = structuredClone(lookupAction) as any;
      action.spec.inputSchema = {
        type: "object",
        required: ["invoice"],
        properties: { invoice: fileSchema },
      };
      await connected(page, {
        capabilities: [...allCapabilities, "file.read", "file.manage"],
        catalog: [{ id: "invoice-action", document: action }],
      });
      const bytes = Buffer.alloc(262147, 65);
      const digest = createHash("sha256").update(bytes).digest("hex");
      let file: any;
      const chunks: { index: number; contentBase64: string }[] = [];
      await page.route("**/environments/development/files", async (route) => {
        file = {
          ...route.request().postDataJSON(),
          kind: "weave/file",
          id: "11111111-1111-4111-8111-111111111111",
        };
        await route.fulfill({
          status: 201,
          json: {
            file,
            state: "uploading",
            received_chunks: [],
            chunk_bytes: 262144,
          },
        });
      });
      await page.route("**/files/*/chunks", async (route) => {
        chunks.push(route.request().postDataJSON());
        await route.fulfill({
          json: {
            file,
            state: "uploading",
            received_chunks: chunks.map((item) => item.index),
            chunk_bytes: 262144,
          },
        });
      });
      await page.route("**/files/*/finish", (route) =>
        route.fulfill({
          json: {
            file,
            state: "ready",
            received_chunks: [0, 1],
            chunk_bytes: 262144,
          },
        }),
      );
      await page.route(
        "**/files/11111111-1111-4111-8111-111111111111",
        (route) =>
          route.fulfill({
            json: {
              file,
              state: "ready",
              received_chunks: [0, 1],
              chunk_bytes: 262144,
            },
          }),
      );
      await page.route("**/files/*/download", (route) => {
        const { index } = route.request().postDataJSON();
        return route.fulfill({
          json: {
            index,
            contentBase64: bytes
              .subarray(index * 262144, (index + 1) * 262144)
              .toString("base64"),
          },
        });
      });
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.append("action");
      await designer.selectStep("call-action-1");
      await chooseAction(page, "sql.lookup@1.0.0");
      const upload = designer.inspector.getByLabel("Upload Invoice", {
        exact: true,
      });
      await upload.setInputFiles({
        name: "invoice.txt",
        mimeType: "text/plain",
        buffer: bytes,
      });
      await expect(designer.inspector.locator(".file-selected")).toContainText(
        "invoice.txt",
      );
      await upload.scrollIntoViewIfNeeded();
      await expectInViewport(upload);
      expect(
        chunks.map((item) => Buffer.from(item.contentBase64, "base64").length),
      ).toEqual([262144, 3]);
      const download = page.waitForEvent("download");
      await designer.inspector
        .getByRole("button", { name: "Download Invoice", exact: true })
        .click();
      expect((await download).suggestedFilename()).toBe("invoice.txt");
      const source = await sourceText(page);
      const value = parse(source).spec.steps[0].with.literal.invoice;
      expect(value).toMatchObject({
        kind: "weave/file",
        filename: "invoice.txt",
        sizeBytes: 262147,
        sha256: digest,
      });
      expect(source).not.toContain("contentBase64");
      expect(source).not.toContain(bytes.toString("base64"));
    });
  });
}

test("a claimed human task uploads without environment file grants and submits only a reference", async ({
  page,
}) => {
  await connected(page, {
    capabilities: [...allCapabilities, "human_task.complete"],
  });
  const task = {
    id: "task-1",
    run_id: "run",
    node_id: "review",
    revision: 7,
    status: "claimed",
    claimant_id: "human",
    title: "Review receipt",
    decisions: ["approve"],
    form_schema: {
      type: "object",
      required: ["receipt"],
      properties: { receipt: fileSchema },
    },
  };
  await page.route("**/human-tasks?*", (route) =>
    route.fulfill({ json: { items: [task], next_cursor: null } }),
  );
  await page.route("**/human-tasks/task-1", (route) =>
    route.fulfill({ json: task }),
  );
  const calls: any[] = [];
  let file: any;
  await page.route("**/human-tasks/task-1/files/*", async (route) => {
    const body = route.request().postDataJSON();
    calls.push({ path: route.request().url(), body });
    if (route.request().url().endsWith("/create"))
      file = {
        ...body.file,
        id: "11111111-1111-4111-8111-111111111111",
        kind: "weave/file",
      };
    await route.fulfill({
      json: {
        file,
        state: route.request().url().endsWith("/finish")
          ? "ready"
          : "uploading",
        received_chunks: [],
        chunk_bytes: 262144,
      },
    });
  });
  let submitted: any;
  await page.route("**/human-tasks/task-1/complete", async (route) => {
    submitted = route.request().postDataJSON();
    await route.fulfill({
      json: { ...task, status: "completed", revision: 8 },
    });
  });
  await page.getByRole("button", { name: "My tasks", exact: true }).click();
  await page.locator(".resource-row").click();
  await page.getByLabel("Upload Receipt", { exact: true }).setInputFiles({
    name: "receipt.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("receipt"),
  });
  await expect(page.locator(".file-selected")).toContainText("receipt.txt");
  expect(calls.map((call) => call.path.split("/").at(-1))).toEqual([
    "create",
    "chunk",
    "finish",
  ]);
  expect(calls.every((call) => call.body.expected_revision === 7)).toBe(true);
  await page.getByRole("button", { name: "Approve", exact: true }).click();
  await page
    .locator(".decision-confirm")
    .getByRole("button", { name: "Approve", exact: true })
    .click();
  await expect.poll(() => submitted).toBeTruthy();
  expect(submitted.data.receipt).toEqual(file);
  expect(JSON.stringify(submitted)).not.toContain("contentBase64");
});
