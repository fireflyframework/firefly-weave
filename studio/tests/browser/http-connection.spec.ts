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
// The weave-http@2.0.0 connection form and readiness checklist (WP-21). The
// Connections view's "New connection" button opens <weave-modal
// heading="New API connection"> holding the lazily loaded
// <weave-http-connection-form>.
import { selectChoice } from "./support";
import { test, expect, Page, Request } from "@playwright/test";
import { allCapabilities, connected, profile } from "./support";

const digest =
  "eddfa829184f8505fd0e1bc7a84b490fc57b555a39495b9724b2728b277133d8";
const connectorVersion = "7a8cbeef-7d5c-483a-b926-4157ad4286d0";
const releaseId = "3f2a9b1c-0000-4000-8000-000000000001";
const revisionId = "0f8fad5b-d9cb-469f-a165-70867728950e";

interface Options {
  capabilities?: string[];
  published?: boolean;
  releases?: boolean;
  local?: boolean;
}
interface Capture {
  creates: Request[];
  tests: Request[];
  lists: number;
}
async function platform(page: Page, options: Options = {}): Promise<Capture> {
  const capture: Capture = { creates: [], tests: [], lists: 0 };
  await connected(page, {
    capabilities: options.capabilities ?? allCapabilities,
  });
  if (options.local) {
    await page.route("**/studio/session", (r) =>
      r.fulfill({
        json: {
          paired: true,
          csrfToken: "test-csrf",
          version: "1",
          mode: "connected",
          profile: { ...profile, baseUrl: "http://127.0.0.1:8080" },
        },
      }),
    );
    await page.reload();
  }
  await page.route("**/projects/project/capabilities", (r) =>
    r.fulfill({ json: { connectors: ["weave-http-v2", "weave-postgresql"] } }),
  );
  await page.route(
    "**/projects/project/connector-descriptors/weave-http-v2",
    (r) =>
      r.fulfill({
        json: {
          adapter: "weave-http-v2",
          reference: "weave-http@2.0.0",
          digest,
          manifest: {},
          source: "{}",
          implementation_version: "2.0.0",
          capabilities: [],
          bindings: [],
          actions: [],
          connection: { config_schema: {}, auth_schema: {} },
          published_version_id:
            options.published === false ? null : connectorVersion,
        },
      }),
  );
  await page.route("**/projects/project/connectors?*", (r) =>
    r.fulfill({ json: { items: [], next_cursor: null } }),
  );
  await page.route("**/environments/development/worker-releases?*", (r) =>
    r.fulfill({
      json: {
        items:
          options.releases === false
            ? []
            : [
                {
                  id: releaseId,
                  image_digest: "sha256:" + "b".repeat(64),
                  capabilities: [
                    {
                      taskType: "weave-connector-http-read",
                      taskVersion: "2.0.0",
                    },
                  ],
                  connector_bindings: [
                    {
                      connector_digest: digest,
                      action: "read",
                      adapter: "weave-http-v2",
                      task_reference: "weave-connector-http-read@2.0.0",
                    },
                  ],
                },
              ],
        next_cursor: null,
      },
    }),
  );
  await page.route("**/environments/development/connections?*", (r) => {
    capture.lists++;
    return r.fulfill({ json: { items: [], next_cursor: null } });
  });
  return capture;
}

async function openForm(page: Page) {
  await page.getByRole("button", { name: "Connections", exact: true }).click();
  await page.getByRole("button", { name: "New connection" }).click();
  const dialog = page.getByRole("dialog", { name: "New API connection" });
  await expect(
    dialog.getByLabel("Connection name", { exact: true }),
  ).toBeVisible();
  return dialog;
}

const oauthBody = {
  name: "pets",
  connector_version_id: connectorVersion,
  config: {
    baseUrl: "https://api.pets.example",
    auth: {
      kind: "machine-token",
      client_id: "studio-client",
      endpoint: "https://login.pets.example/oauth2/token",
      scopes: ["pets.read"],
    },
  },
  secretRef: { client_secret: "pets-client-secret" },
  allowed_destinations: [
    "https://api.pets.example",
    "https://login.pets.example",
  ],
};

async function fillOauth(dialog: ReturnType<Page["getByRole"]>) {
  await dialog.getByLabel("Connection name", { exact: true }).fill("pets");
  await dialog
    .getByLabel("API address", { exact: true })
    .fill("https://api.pets.example");
  await selectChoice(
    dialog.getByLabel("How the API checks who is calling", { exact: true }),
    "machine-token",
  );
  await dialog.getByLabel("Client ID", { exact: true }).fill("studio-client");
  await dialog
    .getByLabel("Token endpoint", { exact: true })
    .fill("https://login.pets.example/oauth2/token");
  await dialog
    .getByLabel("Scopes (optional)", { exact: true })
    .fill("pets.read");
  await dialog
    .getByLabel("Client secret handle", { exact: true })
    .fill("pets-client-secret");
}

for (const [width, height] of [
  [1440, 900],
  [600, 500],
] as const)
  test(`OAuth connection: token endpoint destination, create, then check (${width}x${height})`, async ({
    page,
  }) => {
    test.setTimeout(90_000);
    await page.setViewportSize({ width, height });
    const capture = await platform(page);
    let release = () => {};
    await page.route("**/environments/development/connections", (r) => {
      capture.creates.push(r.request());
      return r.fulfill({
        status: 201,
        json: {
          ...oauthBody,
          id: revisionId,
          revision: 1,
          connector: "weave-http@2.0.0",
        },
      });
    });
    await page.route(`**/connections/${revisionId}/test`, async (r) => {
      capture.tests.push(r.request());
      // Longer than Studio's default 15 s request limit.
      await new Promise<void>((resolve) => {
        release = resolve;
        setTimeout(resolve, 16_000);
      });
      return r.fulfill({ json: { ok: true, code: "ok" } });
    });
    const dialog = await openForm(page);
    await expect(dialog).toContainText("Looks ready.");
    await fillOauth(dialog);
    // The token endpoint origin joins the API origin, both locked.
    const destinations = dialog.locator(".destinations");
    await expect(destinations).toContainText("https://api.pets.example");
    await expect(destinations).toContainText("https://login.pets.example");
    await expect(destinations).toContainText("Token endpoint");
    const create = dialog.getByRole("button", { name: "Create connection" });
    await create.scrollIntoViewIfNeeded();
    await create.click();
    await expect(dialog).toContainText("Created pets (revision 1).");
    expect(capture.creates).toHaveLength(1);
    expect(capture.creates[0].postDataJSON()).toEqual(oauthBody);
    expect(capture.creates[0].headers()["idempotency-key"]).toMatch(
      /^[0-9a-f-]{36}$/,
    );
    const check = dialog.getByRole("button", {
      name: "Check configuration (no request is sent)",
    });
    await expect(check).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(dialog.getByRole("status").last()).toContainText(
      "Checking the configuration",
    );
    await expect(dialog).toContainText(
      "The platform accepted this configuration.",
      {
        timeout: 30_000,
      },
    );
    release();
    // The button stays focusable while the check runs, so focus is still on
    // it afterwards and Escape still reaches the dialog.
    await expect(check).toBeFocused();
    expect(capture.tests).toHaveLength(1);
    expect(capture.tests[0].postDataJSON()).toEqual({});
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
  });

test("an invalid field keeps its red border, and the focus ring clears its label", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await platform(page);
  const dialog = await openForm(page);
  await fillOauth(dialog);
  const secret = dialog.getByLabel("Client secret handle", { exact: true });
  await secret.fill("ghp_abcdefghijklmnopqrstuvwxyz0123456789");
  await dialog.getByRole("button", { name: "Create connection" }).click();
  await expect(secret).toBeFocused();
  await expect(secret).toHaveAttribute("aria-invalid", "true");
  const look = await secret.evaluate((e) => {
    const s = getComputedStyle(e);
    return {
      border: s.borderTopColor,
      bar: s.boxShadow,
      outline: `${s.outlineStyle} ${s.outlineWidth} ${s.outlineColor}`,
      offset: parseFloat(s.outlineOffset),
      width: parseFloat(s.outlineWidth),
    };
  });
  // Red stays red while focused; the green ring is drawn outside it.
  expect(look.border).toBe("rgb(161, 43, 53)");
  expect(look.bar).toContain("rgb(161, 43, 53)");
  expect(look.outline).toBe("solid 2px rgb(44, 106, 87)");
  // The ring's box never covers the field's label.
  const field = (await secret.boundingBox())!;
  const ring = look.offset + look.width;
  const label = (await dialog
    .locator("label", { hasText: "Client secret handle" })
    .first()
    .boundingBox())!;
  const ringTop = field.y - ring;
  const intersects =
    label.x < field.x + field.width + ring &&
    label.x + label.width > field.x - ring &&
    label.y < field.y + field.height + ring &&
    label.y + label.height > ringTop;
  expect(intersects, "the focus ring overlaps the label").toBe(false);
});

test("secret values are refused and server diagnostics land on their fields", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const capture = await platform(page);
  await page.route("**/environments/development/connections", (r) => {
    capture.creates.push(r.request());
    return r.fulfill({
      status: 422,
      json: {
        code: "WV-CONNECTION",
        message: "Connection requirements are unavailable or incompatible",
        diagnostics: [
          {
            code: "WV-CONNECTION-SECRET",
            severity: "error",
            stage: "semantic",
            path: "/secretRef/client_secret",
            message: "This secret handle is not available in this environment.",
          },
          {
            code: "WV-CONNECTION-DESTINATION",
            severity: "error",
            stage: "semantic",
            path: "/allowed_destinations",
            message:
              "Add the token endpoint origin https://login.pets.example to allowed_destinations.",
          },
        ],
      },
    });
  });
  const dialog = await openForm(page);
  await fillOauth(dialog);
  const secret = dialog.getByLabel("Client secret handle", { exact: true });
  await secret.fill("ghp_abcdefghijklmnopqrstuvwxyz0123456789");
  await dialog.getByRole("button", { name: "Create connection" }).click();
  await expect(secret).toBeFocused();
  await expect(secret).toHaveAttribute("aria-invalid", "true");
  await expect(dialog).toContainText("This looks like a secret value.");
  expect(capture.creates).toHaveLength(0);
  await secret.fill("pets-client-secret");
  await expect(secret).not.toHaveAttribute("aria-invalid", "true");
  await dialog.getByRole("button", { name: "Create connection" }).click();
  await expect(secret).toHaveAttribute("aria-invalid", "true");
  await expect(dialog).toContainText(
    "This secret handle is not available in this environment. Check the handle name with your platform operator.",
  );
  await expect(dialog).toContainText(
    "Add the token endpoint origin https://login.pets.example to the allowed destinations.",
  );
  await expect(dialog).toContainText(
    "The platform didn't accept this connection.",
  );
  await expect(dialog).not.toContainText("WV-CONNECTION-SECRET:");
});

test("without connection access, an administrator gets the request and command", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const capture = await platform(page, {
    capabilities: allCapabilities.filter((c) => c !== "connection.manage"),
  });
  await page.route("**/environments/development/connections", (r) => {
    capture.creates.push(r.request());
    return r.fulfill({ status: 403, json: { code: "WV-DENIED" } });
  });
  const dialog = await openForm(page);
  await expect(dialog).toContainText("An administrator creates the connection");
  await fillOauth(dialog);
  await dialog
    .getByRole("button", { name: "Prepare the request for an administrator" })
    .click();
  const request = dialog.getByLabel("Connection request (connection.json)", {
    exact: true,
  });
  await expect(request).toBeFocused();
  expect(JSON.parse(await request.inputValue())).toEqual(oauthBody);
  await expect(dialog.locator(".handoff code")).toHaveText(
    "weave connections create --tenant tenant --project project --environment development --request connection.json",
  );
  expect(capture.creates).toHaveLength(0);
});

test("a lost answer is found by re-listing the name, so nothing is created twice", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const capture = await platform(page);
  let created = false;
  await page.route("**/environments/development/connections?*", (r) => {
    capture.lists++;
    return r.fulfill({
      json: {
        items: created
          ? [
              {
                ...oauthBody,
                id: revisionId,
                revision: 1,
                connector: "weave-http@2.0.0",
              },
            ]
          : [],
        next_cursor: null,
      },
    });
  });
  await page.route("**/environments/development/connections", (r) => {
    capture.creates.push(r.request());
    created = true;
    return r.abort("failed");
  });
  const dialog = await openForm(page);
  await fillOauth(dialog);
  const before = capture.lists;
  await dialog.getByRole("button", { name: "Create connection" }).click();
  await expect(dialog).toContainText("Created pets (revision 1).");
  expect(capture.creates).toHaveLength(1);
  // Listed by name before the create and again after the lost answer.
  expect(capture.lists).toBeGreaterThanOrEqual(before + 2);
});

test("readiness names who acts on the local platform", async ({ page }) => {
  await page.setViewportSize({ width: 600, height: 500 });
  await platform(page, { local: true, published: false, releases: false });
  const dialog = await openForm(page);
  await expect(dialog).toContainText(
    "weave-http@2.0.0 isn't published in this project yet",
  );
  await expect(dialog).toContainText("Who acts: You, on this computer");
  await expect(dialog.locator(".command code").first()).toHaveText(
    "weave platform integrations enable",
  );
  await expect(dialog).toContainText("weave platform secret set --handle NAME");
  // Creating explains why it can't proceed instead of sending a request.
  await fillOauth(dialog);
  const create = dialog.getByRole("button", { name: "Create connection" });
  await create.scrollIntoViewIfNeeded();
  await create.click();
  await expect(dialog.locator(".general-problems")).toContainText(
    "weave-http@2.0.0 isn't published in this project yet.",
  );
});

test("a check that finishes after a newer revision is created is not shown for it", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const capture = await platform(page);
  await page.route("**/environments/development/connections", (r) => {
    capture.creates.push(r.request());
    const first = capture.creates.length === 1;
    return r.fulfill({
      status: 201,
      json: {
        id: first ? revisionId : "0f8fad5b-d9cb-469f-a165-70867728950f",
        name: "pets",
        revision: capture.creates.length,
      },
    });
  });
  let release = () => {};
  let answered = false;
  await page.route(`**/connections/${revisionId}/test`, async (r) => {
    await new Promise<void>((resolve) => (release = resolve));
    await r.fulfill({ json: { ok: true, code: "ok" } });
    answered = true;
  });
  const dialog = await openForm(page);
  await fillOauth(dialog);
  await dialog.getByRole("button", { name: "Create connection" }).click();
  await expect(dialog).toContainText("Created pets (revision 1).");
  await dialog
    .getByRole("button", { name: "Check configuration (no request is sent)" })
    .click();
  await expect(dialog).toContainText("Checking the configuration");
  await dialog
    .getByLabel("Scopes (optional)", { exact: true })
    .fill("pets.write");
  await dialog.getByRole("button", { name: "Create a new revision" }).click();
  await expect(dialog).toContainText("Created pets (revision 2).");
  release();
  await expect.poll(() => answered).toBe(true);
  // Let the late answer reach the page before checking it was dropped.
  await page.waitForTimeout(300);
  await expect(dialog).not.toContainText(
    "The platform accepted this configuration.",
  );
});

test("a refused create shows the administrator request right away", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  // The identity looks allowed, but the platform refuses the create.
  const capture = await platform(page);
  await page.route("**/environments/development/connections", (r) => {
    capture.creates.push(r.request());
    return r.fulfill({ status: 403, json: { code: "WV-DENIED" } });
  });
  const dialog = await openForm(page);
  await fillOauth(dialog);
  await dialog.getByRole("button", { name: "Create connection" }).click();
  await expect(dialog.getByRole("alert")).toContainText(
    "Hand the request below to an administrator.",
  );
  const request = dialog.getByLabel("Connection request (connection.json)", {
    exact: true,
  });
  await expect(request).toBeFocused();
  expect(JSON.parse(await request.inputValue())).toEqual(oauthBody);
  expect(capture.creates).toHaveLength(1);
});

test("creating before the platform check finishes says it is still checking", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const capture = await platform(page);
  let release = () => {};
  await page.route(
    "**/projects/project/connector-descriptors/weave-http-v2",
    async (r) => {
      await new Promise<void>((resolve) => (release = resolve));
      return r.fulfill({
        json: {
          adapter: "weave-http-v2",
          reference: "weave-http@2.0.0",
          digest,
          published_version_id: connectorVersion,
        },
      });
    },
  );
  await page.route("**/environments/development/connections", (r) => {
    capture.creates.push(r.request());
    return r.fulfill({
      status: 201,
      json: { ...oauthBody, id: revisionId, revision: 1 },
    });
  });
  const dialog = await openForm(page);
  await fillOauth(dialog);
  const create = dialog.getByRole("button", { name: "Create connection" });
  await create.click();
  await expect(dialog.locator(".general-problems")).toContainText(
    "Studio is still checking whether weave-http@2.0.0 is published",
  );
  expect(capture.creates).toHaveLength(0);
  release();
  await expect(dialog.locator(".general-problems")).toHaveCount(0);
  await create.click();
  await expect(dialog).toContainText("Created pets (revision 1).");
});

test("the form fits a 360 px wide screen without sideways scrolling", async ({
  page,
}) => {
  await page.setViewportSize({ width: 360, height: 640 });
  await platform(page);
  const dialog = await openForm(page);
  await fillOauth(dialog);
  const overflow = await dialog.evaluate(
    (panel) => panel.scrollWidth - panel.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth - window.innerWidth,
    ),
  ).toBeLessThanOrEqual(0);
});

test("a plain-HTTP API address shows the Not encrypted notice and still creates", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const capture = await platform(page);
  await page.route("**/environments/development/connections", (r) => {
    capture.creates.push(r.request());
    return r.fulfill({
      status: 201,
      json: {
        name: "acme",
        connector_version_id: connectorVersion,
        config: { baseUrl: "http://api.acme.example", auth: { kind: "none" } },
        secretRef: {},
        allowed_destinations: ["http://api.acme.example"],
        id: revisionId,
        revision: 1,
        connector: "weave-http@2.0.0",
      },
    });
  });
  const dialog = await openForm(page);
  await expect(dialog).toContainText("Looks ready.");
  const address = dialog.getByLabel("API address", { exact: true });
  await address.fill("https://api.acme.example");
  await expect(dialog.locator(".plain-http")).toHaveCount(0);
  await address.fill("http://api.acme.example");
  await dialog.getByLabel("Connection name", { exact: true }).fill("acme");
  await expect(dialog.locator(".plain-http")).toHaveText(
    "Not encrypted: requests to http://api.acme.example travel in plain text.",
  );
  // A notice, not an error: the field stays valid and names the notice.
  await expect(address).not.toHaveAttribute("aria-invalid", "true");
  await expect(address).toHaveAttribute("aria-describedby", /-origin-plain/);
  const create = dialog.getByRole("button", { name: "Create connection" });
  await create.scrollIntoViewIfNeeded();
  await create.click();
  await expect(dialog).toContainText("Created acme (revision 1).");
  expect(capture.creates[0].postDataJSON()).toMatchObject({
    config: { baseUrl: "http://api.acme.example", auth: { kind: "none" } },
    allowed_destinations: ["http://api.acme.example"],
  });
});
