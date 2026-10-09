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
import { expect, test } from "@playwright/test";
import { connected } from "./support";

const first = "11111111-1111-4111-8111-111111111111";
const second = "22222222-2222-4222-8222-222222222222";
const target = (id: string, name: string) => ({
  id,
  name,
  adapter: "docker-compose",
  revision: 1,
  disabled: false,
  external_identity: "private-provider-path",
  boundary: "private-boundary",
  runner_principal_id: "33333333-3333-4333-8333-333333333333",
  capabilities: ["observe"],
  created_at: "2026-10-03T10:00:00Z",
});

for (const width of [1440, 390]) {
  test(`Operations explanations opt in to IDs and clear changed context at ${width}`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 900 });
    await connected(page, { capabilities: ["deployment.read", "lumi.use"] });
    const targets = [
      target(first, "First target"),
      target(second, "Second target"),
    ];
    await page.route("**/deployment-targets?*", (route) =>
      route.fulfill({ json: { items: targets, next_cursor: null } }),
    );
    for (const item of targets)
      await page.route("**/deployment-targets/" + item.id, (route) =>
        route.fulfill({ json: item }),
      );
    await page.route("**/lumi/status", (route) =>
      route.fulfill({
        json: { configured: true, provider: "fixture", model: "fixture" },
      }),
    );
    const sent: any[] = [];
    const mutations: string[] = [];
    page.on("request", (request) => {
      if (
        request.method() !== "GET" &&
        /deployment-(plans|jobs|observations|targets)/.test(request.url())
      )
        mutations.push(request.url());
    });
    await page.route("**/lumi/ask", async (route) => {
      sent.push(route.request().postDataJSON());
      await route.fulfill({
        json: {
          answer: "Saved record explanation",
          proposals: [
            {
              title: "Unexpected proposal",
              kind: "workflow",
              format: "yaml",
              source: "kind: Workflow",
            },
          ],
          followUps: [],
        },
      });
    });
    await page.getByRole("button", { name: "Clusters", exact: true }).click();
    await page
      .getByRole("button", { name: "First target", exact: true })
      .click();
    await page
      .getByRole("button", { name: "Explain with Weave AI", exact: true })
      .click();
    const panel = page.locator("weave-lumi-panel");
    await expect(
      panel.getByLabel("Include selected target", { exact: true }),
    ).not.toBeChecked();
    await expect(
      panel.getByText("No deployment changes are made.", { exact: false }),
    ).toBeVisible();
    expect(sent).toEqual([]);
    await panel
      .getByLabel("Message to Weave AI", { exact: true })
      .fill("Explain generally");
    await panel
      .getByRole("button", { name: "Send message", exact: true })
      .click();
    await expect(
      panel.getByText("Saved record explanation", { exact: true }),
    ).toBeVisible();
    expect(sent[0].attachments).toEqual([]);
    await expect(
      panel.getByRole("button", { name: "Unexpected proposal", exact: true }),
    ).toHaveCount(0);
    await panel.getByLabel("Include selected target", { exact: true }).check();
    await panel
      .getByLabel("Message to Weave AI", { exact: true })
      .fill("Explain this target");
    await panel
      .getByRole("button", { name: "Send message", exact: true })
      .click();
    await expect.poll(() => sent.length).toBe(2);
    await expect(
      panel.getByText("Saved record explanation", { exact: true }),
    ).toHaveCount(2);
    expect(sent[1].attachments).toEqual([
      { kind: "deployment-target", id: first },
    ]);
    expect(JSON.stringify(sent[1])).not.toContain("private-");
    await panel
      .getByRole("button", { name: "Close Weave AI", exact: true })
      .click();
    await page
      .getByRole("button", { name: "Back to Clusters", exact: true })
      .click();
    await page
      .getByRole("button", { name: "Second target", exact: true })
      .click();
    await page
      .getByRole("button", { name: "Explain with Weave AI", exact: true })
      .click();
    await expect(
      panel.getByLabel("Include selected target", { exact: true }),
    ).not.toBeChecked();
    await expect(
      panel.getByText("Saved record explanation", { exact: true }),
    ).toHaveCount(0);
    expect(mutations).toEqual([]);
  });
}
