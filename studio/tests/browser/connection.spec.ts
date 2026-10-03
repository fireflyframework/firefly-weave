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
// Connection wizard, saved platforms and startup reconnection against a
// route-mocked local host that follows the design contract (section 5).
import { test, expect, Page } from "@playwright/test";
import {
  PlatformHost,
  acme,
  acmeDiscovery,
  acmeOption,
  acmePlatform,
  expectLabelledControls,
  expectNoOverflow,
  globexPlatform,
  manyWorkspaces,
  partnerOption,
  platformHost,
} from "./platform-host";

const heading = (page: Page) => page.locator("#wizard-heading");
const wizard = (page: Page) => page.locator("weave-connection-wizard");
const login = 15_000;

async function openConnect(page: Page) {
  await page
    .locator("weave-home-dashboard")
    .getByRole("button", { name: "Connect to a platform" })
    .click();
  await expect(heading(page)).toHaveText("Connect to a platform");
}
async function discoverAcme(page: Page, address = "weave.acme.example") {
  await page.getByLabel("Server address").fill(address);
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(heading(page)).toHaveText("Review and trust");
}
async function trustAndContinue(page: Page) {
  await page.getByLabel("I trust this server and identity provider").check();
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(heading(page)).toHaveText(/^Sign in to /);
}
const discovery = { "weave.acme.example": { json: acmeDiscovery } };

test.describe("choosing how to work", () => {
  test("first run asks how to work and remembers working locally", async ({
    page,
  }) => {
    const host = await platformHost(page, { firstRun: true });
    await expect(heading(page)).toHaveText("How do you want to work?");
    await expect(heading(page)).toBeFocused();
    const local = page.getByRole("button", { name: "Work locally" });
    await expect(local).toHaveAccessibleDescription(/No account needed/);
    await expect(
      page.getByRole("button", { name: "Connect to a platform" }),
    ).toHaveAccessibleDescription(/publish, run, and manage work/);
    await local.click();
    await expect(page.locator("weave-home-dashboard")).toBeVisible();
    await expect(page.locator(".dashboard-status")).toHaveText(
      "Local authoring",
    );
    expect(host.requests("/studio/connection/disconnect")).toHaveLength(0);
    // The host keeps the choice next to the saved platforms, not the browser.
    await expect
      .poll(() => host.requests("/studio/preferences").map((c) => c.body))
      .toEqual([{ start: "local" }]);
    expect(host.preferences).toEqual({ start: "local" });
    expect(host.csrfViolations).toEqual([]);
    expect(await page.evaluate(() => Object.keys(localStorage))).not.toContain(
      "weave-studio-start",
    );
    await page.reload();
    await expect(page.locator("weave-home-dashboard")).toBeVisible();
    await expect(wizard(page)).toHaveCount(0);
  });

  test("a host that already starts locally skips the first-run choice", async ({
    page,
  }) => {
    const host = await platformHost(page);
    expect(host.preferences.start).toBe("local");
    await expect(page.locator("weave-home-dashboard")).toBeVisible();
    await expect(page.locator(".dashboard-status")).toHaveText(
      "Local authoring",
    );
    await expect(wizard(page)).toHaveCount(0);
    expect(host.requests("/studio/preferences")).toHaveLength(0);
  });

  test("Settings chooses whether Studio asks how to work when it opens", async ({
    page,
  }) => {
    const host = await platformHost(page);
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    const choice = page.getByRole("radiogroup", { name: "When Studio opens" });
    await expect(
      choice.getByRole("radio", { name: "Start with local authoring" }),
    ).toBeChecked();
    await choice.getByRole("radio", { name: "Ask how to work" }).check();
    await expect
      .poll(() => host.requests("/studio/preferences").map((c) => c.body))
      .toEqual([{ start: "ask" }]);
    await expect(choice.getByRole("status")).toHaveText("Saved.");
    expect(host.preferences).toEqual({ start: "ask" });
    await page.goto("/");
    await expect(heading(page)).toHaveText("How do you want to work?");
    await page.getByRole("button", { name: "Work locally" }).click();
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await expect(
      choice.getByRole("radio", { name: "Start with local authoring" }),
    ).toBeChecked();
    expect(host.requests("/studio/preferences").map((c) => c.body)).toEqual([
      { start: "ask" },
      { start: "local" },
    ]);
  });

  test("a start preference the host can't save is reported and undone", async ({
    page,
  }) => {
    await platformHost(page, { preferencesStatus: 503 });
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    const choice = page.getByRole("radiogroup", { name: "When Studio opens" });
    await choice.getByRole("radio", { name: "Ask how to work" }).check();
    const alert = choice.getByRole("alert");
    // One plain sentence, with the support code in small print.
    await expect(alert).toHaveText(
      "Studio couldn't save this setting. Check that your user configuration folder is writable, then try again. Support code: WV-PROFILE-STORE",
    );
    await expect(alert.locator(".support-code")).toHaveText(
      "Support code: WV-PROFILE-STORE",
    );
    await expect(
      choice.getByRole("radio", { name: "Start with local authoring" }),
    ).toBeChecked();
  });

  test("changing the start preference with the keyboard keeps focus on the choice", async ({
    page,
  }) => {
    const host = await platformHost(page);
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    const choice = page.getByRole("radiogroup", { name: "When Studio opens" });
    await choice
      .getByRole("radio", { name: "Start with local authoring" })
      .focus();
    await page.keyboard.press("ArrowUp");
    await expect
      .poll(() => host.requests("/studio/preferences").map((c) => c.body))
      .toEqual([{ start: "ask" }]);
    await expect(choice.getByRole("status")).toHaveText("Saved.");
    const ask = choice.getByRole("radio", { name: "Ask how to work" });
    await expect(ask).toBeChecked();
    await expect(ask).toBeFocused();
    // Quick changes in a row each reach the host; the last one wins.
    await page.keyboard.press("ArrowDown");
    await page.keyboard.press("ArrowUp");
    await page.keyboard.press("ArrowDown");
    await expect
      .poll(() => host.requests("/studio/preferences").length)
      .toBe(4);
    await expect(choice.getByRole("status")).toHaveText("Saved.");
    expect(host.preferences).toEqual({ start: "local" });
    await expect(
      choice.getByRole("radio", { name: "Start with local authoring" }),
    ).toBeFocused();
  });

  test("connecting from the first-run choice continues to the server step", async ({
    page,
  }) => {
    await platformHost(page, { firstRun: true });
    await page.getByRole("button", { name: "Connect to a platform" }).click();
    await expect(heading(page)).toHaveText("Connect to a platform");
    await expect(heading(page)).toBeFocused();
    await expect(
      page.getByRole("list", { name: "Connection progress" }).locator("li"),
    ).toHaveText([/Server/, /Review/, /Sign in/, /Workspace/]);
    await expect(
      page.locator(".wizard-progress li[aria-current='step']"),
    ).toContainText("Server");
    await page.getByRole("button", { name: "Back" }).click();
    await expect(heading(page)).toHaveText("How do you want to work?");
  });
});

test.describe("server step", () => {
  const failures: [string, number, string, RegExp][] = [
    [
      "WV-CONNECT-ADDRESS",
      422,
      "doesn't look like a server address",
      /Leave out paths/,
    ],
    ["WV-CONNECT-BLOCKED", 422, "can't connect to that address", /reserved/],
    ["WV-CONNECT-UNREACHABLE", 502, "couldn't reach", /VPN/],
    ["WV-CONNECT-TLS", 502, "secure connection", /certificate/],
    ["WV-CONNECT-TIMEOUT", 502, "didn't answer in time", /VPN/],
    [
      "WV-CONNECT-NOT-WEAVE",
      502,
      "doesn't look like a Firefly Weave server",
      /not a sign-in page/,
    ],
    [
      "WV-CONNECT-INCOMPATIBLE",
      502,
      "version of Firefly Weave",
      /Update Firefly Weave Studio/,
    ],
    ["WV-CONNECT-PROVIDER", 502, "identity provider", /administrator/],
  ];
  for (const [code, status, title, hint] of failures)
    test(`explains ${code} in plain language`, async ({ page }) => {
      await platformHost(page, {
        discover: {
          "bad.example": {
            status,
            json: { status, code, message: "private technical detail" },
          },
        },
      });
      await openConnect(page);
      await page.getByLabel("Server address").fill("bad.example");
      await page.keyboard.press("Enter");
      const problem = page.locator("#wizard-server-problem");
      await expect(problem).toBeVisible();
      await expect(problem.locator("strong")).toContainText(title);
      await expect(problem.locator("strong")).not.toContainText("WV-");
      await expect(problem).toContainText(hint);
      await expect(problem.locator(".support-code")).toHaveText(
        `Support code: ${code}`,
      );
      await expect(page.getByText("private technical detail")).toHaveCount(0);
      await expect(page.getByLabel("Server address")).toHaveAttribute(
        "aria-describedby",
        /wizard-server-problem/,
      );
    });

  test("suggests HTTPS and the redirect target, then retries with it", async ({
    page,
  }) => {
    const host = await platformHost(page, {
      discover: {
        "http://weave.acme.example": {
          status: 422,
          json: { status: 422, code: "WV-CONNECT-INSECURE", message: "x" },
        },
        "https://weave.acme.example": {
          status: 409,
          json: {
            status: 409,
            code: "WV-CONNECT-REDIRECT",
            message: "That address forwards to https://api.acme.example.",
            suggested_server: "https://api.acme.example",
          },
        },
        "https://api.acme.example": {
          json: { ...acmeDiscovery, server: "https://api.acme.example" },
        },
      },
    });
    await openConnect(page);
    await page.getByLabel("Server address").fill("http://weave.acme.example");
    await page.getByRole("button", { name: "Continue" }).click();
    await expect(page.locator("#wizard-server-problem")).toContainText(
      "This address isn't secure.",
    );
    await page
      .getByRole("button", { name: "Use https://weave.acme.example" })
      .click();
    await expect(page.locator("#wizard-server-problem")).toContainText(
      "forwards to another location",
    );
    await page
      .getByRole("button", { name: "Use https://api.acme.example" })
      .click();
    await expect(heading(page)).toHaveText("Review and trust");
    expect(
      host.requests("/studio/connection/discover").map((c) => c.body),
    ).toEqual([
      { server: "http://weave.acme.example" },
      { server: "https://weave.acme.example" },
      { server: "https://api.acme.example" },
    ]);
  });

  test("validates the address, shows progress and can cancel a slow check", async ({
    page,
  }) => {
    await platformHost(page, {
      discover: { "slow.example": { json: acmeDiscovery, delay: 3000 } },
    });
    await openConnect(page);
    await page.getByRole("button", { name: "Continue" }).click();
    const address = page.getByLabel("Server address");
    await expect(page.locator("#wizard-server-error")).toHaveText(
      "Enter the server address.",
    );
    await expect(address).toBeFocused();
    await expect(address).toHaveAttribute("aria-invalid", "true");
    await expect(address).toHaveAttribute(
      "aria-describedby",
      /wizard-server-error/,
    );
    await expect(address).toHaveAttribute("inputmode", "url");
    await expect(address).toHaveAttribute("autocapitalize", "off");
    await expect(address).toHaveAttribute("spellcheck", "false");
    await expect(address).toHaveAttribute(
      "placeholder",
      "https://weave.example.com",
    );
    await address.fill("slow.example");
    await address.press("Enter");
    const checking = page.getByRole("status").filter({ hasText: "Checking" });
    await expect(checking).toContainText("Checking slow.example");
    await checking.getByRole("button", { name: "Cancel" }).click();
    await expect(checking).toHaveCount(0);
    await page.waitForTimeout(3300);
    await expect(heading(page)).toHaveText("Connect to a platform");
  });

  test("no published sign-in points to a connection file", async ({ page }) => {
    await platformHost(page, {
      discover: {
        "old.example": {
          status: 422,
          json: { status: 422, code: "WV-CONNECT-NO-SIGN-IN", message: "x" },
        },
      },
    });
    await openConnect(page);
    await page.getByLabel("Server address").fill("old.example");
    await page.keyboard.press("Enter");
    const problem = page.locator("#wizard-server-problem");
    await expect(problem).toContainText(
      "Ask your administrator for a connection file",
    );
    await problem
      .getByRole("button", { name: "Use a connection file" })
      .click();
    await expect(heading(page)).toHaveText("Use a connection file");
  });
});

test.describe("review and trust", () => {
  test("requires trust and a valid name before saving the reviewed platform", async ({
    page,
  }) => {
    const host = await platformHost(page, {
      platforms: [acmePlatform({ name: "Taken", signedIn: false })],
      discover: {
        "weave.acme.example": {
          json: {
            ...acmeDiscovery,
            existing_profile: "Taken",
            sign_in: [acmeOption, partnerOption],
          },
        },
      },
    });
    await openConnect(page);
    await discoverAcme(page);
    await expect(heading(page)).toBeFocused();
    const review = wizard(page);
    await expect(review).toContainText("Acme Weave");
    await expect(review).toContainText("https://weave.acme.example");
    await expect(review).toContainText(
      "You will sign in with Acme (Microsoft Entra ID) at login.acme.example.",
    );
    await expect(review).toContainText(
      "Your web browser or a code on another device",
    );
    await expect(
      page.getByRole("button", { name: "Use saved platform Taken" }),
    ).toBeVisible();
    const providers = page.getByRole("group", { name: "Identity provider" });
    await expect(
      providers.getByRole("radio", { name: /Acme \(Microsoft Entra ID\)/ }),
    ).toBeChecked();
    const partner = providers.getByRole("radio", { name: /Partner accounts/ });
    await expect(partner).toBeDisabled();
    await expect(providers).toContainText(
      "Studio could not reach this sign-in service.",
    );
    const name = page.getByLabel("Name", { exact: true });
    await expect(name).toHaveValue("Acme Weave");
    await page.getByText("Advanced", { exact: true }).click();
    await expect(review).toContainText("weave-studio");
    await expect(review).toContainText("openid profile offline_access");
    await expect(review).toContainText("https://device.acme.example");

    await page.getByRole("button", { name: "Continue" }).click();
    const trust = page.getByLabel("I trust this server and identity provider");
    await expect(page.locator("#wizard-trust-error")).toHaveText(
      "Confirm that you trust this server and identity provider to continue.",
    );
    await expect(trust).toBeFocused();
    await expect(trust).toHaveAttribute(
      "aria-describedby",
      "wizard-trust-hint wizard-trust-error",
    );
    expect(host.requests("/studio/connection/profiles")).toHaveLength(0);

    await name.fill("Acme/prod ");
    await trust.check();
    await page.getByRole("button", { name: "Continue" }).click();
    await expect(page.locator("#wizard-name-error")).toContainText(
      "Start with a letter or digit",
    );
    await expect(name).toBeFocused();
    await name.fill("Taken");
    await page.getByRole("button", { name: "Continue" }).click();
    await expect(page.locator("#wizard-name-error")).toHaveText(
      "You already have a platform named Taken. Choose another name.",
    );
    await name.fill("Acme production");
    await page.getByRole("button", { name: "Continue" }).click();
    await expect(heading(page)).toHaveText("Sign in to Acme production");
    expect(host.requests("/studio/connection/profiles").at(-1)!.body).toEqual({
      name: "Acme production",
      server: "https://weave.acme.example",
      provider_id: "acme",
      issuer: acmeOption.issuer,
      client_id: "weave-studio",
      trust_confirmed: true,
    });
    expect(host.csrfViolations).toEqual([]);
    await expect(page.locator(".platform-indicator")).toHaveAccessibleName(
      /^Platform: Acme production · /,
    );
  });

  test("a changed server announcement asks for a new review", async ({
    page,
  }) => {
    await platformHost(page, {
      discover: {
        "weave.acme.example": {
          json: {
            ...acmeDiscovery,
            sign_in: [{ ...acmeOption, client_id: "renamed" }],
          },
        },
      },
    });
    await openConnect(page);
    await discoverAcme(page);
    await page.getByLabel("I trust this server and identity provider").check();
    await page.getByRole("button", { name: "Continue" }).click();
    const problem = page.locator("#wizard-save-problem");
    await expect(problem).toContainText(
      "The server's sign-in settings changed.",
    );
    await expect(problem).toContainText("Support code: WV-PROFILE-CHANGED");
    await expect(
      problem.getByRole("button", { name: "Review again" }),
    ).toBeVisible();
  });

  test("a connection file is still validated and rejects tokens and secrets", async ({
    page,
  }) => {
    const host = await platformHost(page, { discover: discovery });
    await openConnect(page);
    await discoverAcme(page);
    await page.getByText("Advanced", { exact: true }).click();
    await page
      .getByRole("button", { name: "Use a connection file instead" })
      .click();
    await expect(heading(page)).toHaveText("Use a connection file");
    await expect(heading(page)).toBeFocused();
    const file = page.getByLabel("Connection file", { exact: true });
    await file.setInputFiles({
      name: "secret.json",
      mimeType: "application/json",
      buffer: Buffer.from(JSON.stringify({ access_token: "never-import" })),
    });
    await expect(page.locator("#wizard-file-error")).toContainText(
      "Tokens, secrets, and credential file paths are not accepted.",
    );
    await file.setInputFiles({
      name: "named.json",
      mimeType: "application/json",
      buffer: Buffer.from(
        JSON.stringify({
          name: "x",
          login: { provider_id: "a" },
          refresh_token: "never-import",
        }),
      ),
    });
    await expect(page.locator("#wizard-file-error")).toContainText(
      "Tokens and secrets are not accepted.",
    );
    await expect(page.getByLabel("Name", { exact: true })).toHaveCount(0);
    await page.getByRole("button", { name: "Continue" }).click();
    await expect(page.locator("#wizard-file-button")).toBeFocused();
    expect(host.requests("/studio/connection/configure")).toHaveLength(0);

    await file.setInputFiles({
      name: "acme-operator.json",
      mimeType: "application/json",
      buffer: Buffer.from(
        JSON.stringify({
          provider_id: "acme",
          issuer: "https://login.acme.example/tenant/v2.0",
          client_id: "weave-cli",
          target: "https://weave.acme.example",
          account: "operator",
        }),
      ),
    });
    await expect(page.locator("#wizard-file-error")).toHaveCount(0);
    await expect(wizard(page)).toContainText("weave-cli");
    await expect(page.getByLabel("Name", { exact: true })).toHaveValue(
      "acme-operator",
    );
    await page.getByRole("button", { name: "Continue" }).click();
    await expect(page.locator("#wizard-trust-error")).toBeVisible();
    await trustAndContinue(page);
    expect(host.requests("/studio/connection/configure")[0].body).toEqual({
      name: "acme-operator",
      login: {
        scopes: ["openid", "profile", "email"],
        provider_id: "acme",
        issuer: "https://login.acme.example/tenant/v2.0",
        client_id: "weave-cli",
        target: "https://weave.acme.example",
        account: "operator",
      },
      trust_confirmed: true,
    });
  });
});

test.describe("sign-in", () => {
  async function toSignIn(page: Page, options = {}) {
    const host = await platformHost(page, { discover: discovery, ...options });
    await openConnect(page);
    await discoverAcme(page);
    await trustAndContinue(page);
    return host;
  }

  test("browser sign-in shows the address, copies it and continues to workspaces", async ({
    page,
    context,
  }) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    const host = await toSignIn(page);
    await expect(heading(page)).toBeFocused();
    await expect(wizard(page)).toContainText(
      "Your password stays with login.acme.example",
    );
    await page
      .getByRole("button", { name: "Sign in with your browser" })
      .click();
    await expect(
      page.getByText("Waiting for you to finish signing in…"),
    ).toBeVisible({
      timeout: login,
    });
    const link = page.getByRole("link", { name: "Open sign-in page" });
    await expect(link).toHaveAttribute("target", "_blank");
    await expect(link).toHaveAttribute("rel", /noopener/);
    await expect(link).toHaveAttribute(
      "href",
      /^https:\/\/login\.acme\.example\/authorize/,
    );
    await expect(
      page.getByRole("textbox", { name: "Sign-in address" }),
    ).toHaveValue(/^https:\/\/login\.acme\.example\/authorize/);
    // The host states when it stops waiting (310 seconds from the start).
    await expect(wizard(page)).toContainText(
      "About 5 minutes left to finish signing in.",
    );
    await page.getByRole("button", { name: "Copy sign-in address" }).click();
    await expect(page.getByText("Copied.")).toBeVisible();
    expect(await page.evaluate(() => navigator.clipboard.readText())).toMatch(
      /^https:\/\/login\.acme\.example\/authorize/,
    );
    expect(host.requests("/studio/connection/login/start")[0].body).toEqual({
      flow: "browser",
      switch_account: false,
    });
    host.finish("authenticated");
    await expect(heading(page)).toHaveText("Choose a workspace", {
      timeout: login,
    });
    await expect(heading(page)).toBeFocused();
    await expect(page.locator(".wizard-progress li.complete")).toHaveCount(3);
  });

  test("copy falls back to selecting the text with an instruction", async ({
    page,
  }) => {
    await page.addInitScript(() => {
      Object.defineProperty(navigator, "clipboard", {
        value: { writeText: () => Promise.reject(new Error("denied")) },
      });
      document.execCommand = () => false;
    });
    await toSignIn(page);
    await page
      .getByRole("button", { name: "Sign in with your browser" })
      .click();
    const address = page.getByRole("textbox", { name: "Sign-in address" });
    await expect(address).toBeVisible({ timeout: login });
    await page.getByRole("button", { name: "Copy sign-in address" }).click();
    await expect(
      page.getByRole("status").filter({ hasText: "Ctrl+C" }),
    ).toContainText("The text is selected");
    await expect(address).toBeFocused();
    expect(
      await address.evaluate(
        (e: HTMLInputElement) => e.selectionEnd! - e.selectionStart!,
      ),
    ).toBeGreaterThan(20);
  });

  test("device code sign-in shows the code large with copy", async ({
    page,
  }) => {
    const host = await toSignIn(page);
    await page.getByRole("button", { name: "Use a code instead" }).click();
    const code = page.getByRole("status", { name: "Sign-in code" });
    await expect(code).toHaveText("WDJB-MJHT", { timeout: login });
    expect(
      await code.evaluate((e) => parseFloat(getComputedStyle(e).fontSize)),
    ).toBeGreaterThanOrEqual(22);
    await expect(wizard(page)).toContainText("device.acme.example");
    await expect(page.getByRole("button", { name: "Copy code" })).toBeVisible();
    expect(host.requests("/studio/connection/login/start")[0].body).toEqual({
      flow: "device",
      switch_account: false,
    });
    host.finish("authenticated");
    await expect(heading(page)).toHaveText("Choose a workspace", {
      timeout: login,
    });
  });

  test("cancel stops the host flow and offers Try again", async ({ page }) => {
    const host = await toSignIn(page);
    await page
      .getByRole("button", { name: "Sign in with your browser" })
      .click();
    await expect(
      page.getByRole("textbox", { name: "Sign-in address" }),
    ).toBeVisible({
      timeout: login,
    });
    await page.getByRole("button", { name: "Cancel sign-in" }).click();
    // Canceling is not a problem: a neutral note, not an alert.
    await expect(page.locator("#wizard-sign-in-problem")).toHaveAttribute(
      "role",
      "status",
    );
    await expect(page.locator("#wizard-sign-in-problem")).toContainText(
      "Sign-in canceled.",
    );
    await expect(page.locator("#wizard-sign-in-problem")).toContainText(
      "Nothing changed.",
    );
    expect(
      host.requests("/studio/connection/login/login-1/cancel"),
    ).toHaveLength(1);
    await page.getByRole("button", { name: "Try again" }).click();
    await expect(
      page.getByRole("textbox", { name: "Sign-in address" }),
    ).toBeVisible({
      timeout: login,
    });
    expect(host.requests("/studio/connection/login/start")).toHaveLength(2);
  });

  test("a provider without code sign-in retries in the browser", async ({
    page,
  }) => {
    const host = await toSignIn(page);
    await page.getByRole("button", { name: "Use a code instead" }).click();
    await expect(
      page.getByRole("status", { name: "Sign-in code" }),
    ).toBeVisible({ timeout: login });
    host.finish("failed", "WV-AUTH-DEVICE-UNAVAILABLE");
    const alert = page.locator("#wizard-sign-in-problem");
    await expect(alert).toContainText(
      "This identity provider doesn't offer sign-in with a code",
      { timeout: login },
    );
    await expect(alert.locator(".support-code")).toHaveText(
      "Support code: WV-AUTH-DEVICE-UNAVAILABLE",
    );
    await page.getByRole("button", { name: "Try again" }).click();
    await expect(
      page.getByRole("textbox", { name: "Sign-in address" }),
    ).toBeVisible({ timeout: login });
    expect(
      host.requests("/studio/connection/login/start").map((c) => c.body),
    ).toEqual([
      { flow: "device", switch_account: false },
      { flow: "browser", switch_account: false },
    ]);
    await page.getByRole("button", { name: "Cancel sign-in" }).click();
    await expect(page.getByRole("button", { name: "Try again" })).toBeVisible();
    await page.getByRole("button", { name: "Back" }).click();
    await page.getByRole("button", { name: "Continue" }).click();
    await expect(heading(page)).toHaveText(/^Sign in to /);
    await expect(
      page.getByRole("button", { name: "Sign in with your browser" }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Use a code instead" }),
    ).toHaveCount(0);
  });

  for (const [code, title] of [
    ["WV-AUTH-EXPIRED", "Sign-in timed out"],
    ["WV-AUTH-DENIED", "Sign-in was declined"],
    ["WV-AUTH-STORE", "Studio couldn't save your sign-in"],
    ["WV-AUTH-PROVIDER", "Sign-in didn't work"],
  ])
    test(`a failed sign-in (${code}) explains what happened`, async ({
      page,
    }) => {
      const host = await toSignIn(page);
      await page
        .getByRole("button", { name: "Sign in with your browser" })
        .click();
      await expect(
        page.getByRole("textbox", { name: "Sign-in address" }),
      ).toBeVisible({
        timeout: login,
      });
      host.finish("failed", code);
      const alert = page.locator("#wizard-sign-in-problem");
      await expect(alert.locator("strong")).toHaveText(title, {
        timeout: login,
      });
      await expect(alert.locator("strong")).not.toContainText("WV-");
      await expect(alert.locator(".support-code")).toHaveText(
        `Support code: ${code}`,
      );
      await expect(
        page.getByRole("button", { name: "Try again" }),
      ).toBeVisible();
      await expect(page.getByRole("button", { name: "Back" })).toBeVisible();
    });

  test("without an expiry from the host Studio promises no time", async ({
    page,
  }) => {
    await toSignIn(page, { loginExpiresIn: null });
    await page
      .getByRole("button", { name: "Sign in with your browser" })
      .click();
    await expect(wizard(page)).toContainText(
      "Finish signing in within a few minutes.",
      { timeout: login },
    );
    await expect(wizard(page)).not.toContainText("left to finish signing in");
  });

  test("Studio stops waiting at the host's sign-in expiry", async ({
    page,
  }) => {
    const host = await toSignIn(page, { loginExpiresIn: 2 });
    await page
      .getByRole("button", { name: "Sign in with your browser" })
      .click();
    await expect(
      page.getByText("Less than a minute left to finish signing in."),
    ).toBeVisible({
      timeout: login,
    });
    await expect(page.locator("#wizard-sign-in-problem")).toContainText(
      "Sign-in timed out",
      { timeout: login },
    );
    await expect
      .poll(
        () => host.requests("/studio/connection/login/login-1/cancel").length,
      )
      .toBe(1);
  });

  test("a pending sign-in resumes after a reload", async ({ page }) => {
    const host = await platformHost(page, {
      platforms: [acmePlatform({ signedIn: false })],
      active: "Acme",
      pendingLogin: { flow: "device" },
    });
    await expect(heading(page)).toHaveText("Sign in to Acme");
    await expect(page.getByRole("status", { name: "Sign-in code" })).toHaveText(
      "WDJB-MJHT",
      { timeout: login },
    );
    host.finish("authenticated");
    await expect(heading(page)).toHaveText("Choose a workspace", {
      timeout: login,
    });
    expect(host.requests("/studio/connection/login/start")).toHaveLength(0);
  });

  test("the desktop shell asks the host to open the browser again", async ({
    page,
  }) => {
    await page.addInitScript(() => {
      (window as unknown as Record<string, unknown>)["__TAURI_INTERNALS__"] =
        {};
    });
    const host = await toSignIn(page);
    await page
      .getByRole("button", { name: "Sign in with your browser" })
      .click();
    await expect(wizard(page)).toContainText(
      "Studio opened the sign-in page in your web browser.",
      { timeout: login },
    );
    await expect(
      page.getByRole("link", { name: "Open sign-in page" }),
    ).toHaveCount(0);
    await page.getByRole("button", { name: "Open again" }).click();
    await expect
      .poll(() => host.requests("/studio/connection/login/login-1/open").length)
      .toBe(1);
  });
});

test.describe("workspace step", () => {
  test("picks a grouped workspace, saves it and starts working", async ({
    page,
  }) => {
    const host = await platformHost(page, {
      platforms: [acmePlatform({ workspace: null })],
      active: "Acme",
    });
    await expect(page.locator(".platform-banner")).toContainText(
      "Choose a workspace on Acme",
    );
    await page.getByRole("button", { name: "Choose workspace" }).click();
    await expect(heading(page)).toHaveText("Choose a workspace");
    const tenant = page.getByRole("group", { name: "Acme", exact: true });
    await expect(
      tenant.getByRole("group", { name: "Payments", exact: true }),
    ).toBeVisible();
    await expect(
      tenant.getByRole("group", { name: "Billing", exact: true }),
    ).toBeVisible();
    await expect(page.getByLabel("Search workspaces")).toHaveCount(0);
    await page.getByRole("button", { name: "Start working" }).click();
    await expect(page.locator("#wizard-workspace-error")).toHaveText(
      "Choose a workspace to continue.",
    );
    await page
      .getByRole("radio", { name: "Acme / Payments / Staging" })
      .check();
    await page.getByRole("button", { name: "Start working" }).click();
    await expect(page.locator("weave-home-dashboard")).toBeVisible();
    expect(host.requests("/studio/scope")[0].body).toEqual({
      tenantId: acme[1].tenant_id,
      projectId: acme[1].project_id,
      environmentId: acme[1].environment_id,
    });
    await expect(page.locator(".platform-indicator")).toHaveAccessibleName(
      "Platform: Acme · Signed in as jane@acme.example · Workspace Payments / Staging",
    );
    await expect(page.locator(".platform-indicator")).toContainText(
      "Payments / Staging",
    );
  });

  test("preselects the saved workspace and searches long lists", async ({
    page,
  }) => {
    await platformHost(page, {
      platforms: [
        acmePlatform({
          workspace: manyWorkspaces[5],
          workspaces: manyWorkspaces,
        }),
      ],
      active: "Acme",
    });
    await page.locator(".platform-indicator").click();
    await page.getByRole("button", { name: "Switch workspace" }).click();
    await expect(
      page.getByRole("radio", { name: "Globex / Edge / Production" }),
    ).toBeChecked();
    const search = page.getByLabel("Search workspaces");
    await search.fill("initech stag");
    await expect(page.getByRole("radio")).toHaveCount(1);
    await expect(page.getByRole("radio")).toHaveAccessibleName(
      "Initech / Reports / Staging",
    );
    await search.fill("nothing like this");
    await expect(wizard(page)).toContainText("No workspace matches");
  });

  test("Enter in the workspace search selects a single match without starting", async ({
    page,
  }) => {
    const host = await platformHost(page, {
      platforms: [
        acmePlatform({
          workspace: manyWorkspaces[5],
          workspaces: manyWorkspaces,
        }),
      ],
      active: "Acme",
    });
    await page.locator(".platform-indicator").click();
    await page.getByRole("button", { name: "Switch workspace" }).click();
    const search = page.getByLabel("Search workspaces");
    await search.fill("initech stag");
    await search.press("Enter");
    await expect(heading(page)).toHaveText("Choose a workspace");
    await expect(
      page.getByRole("radio", { name: "Initech / Reports / Staging" }),
    ).toBeChecked();
    await search.fill("production");
    await search.press("Enter");
    await expect(heading(page)).toHaveText("Choose a workspace");
    expect(host.requests("/studio/scope")).toHaveLength(0);
    await page.getByRole("button", { name: "Start working" }).click();
    await expect(page.locator("weave-home-dashboard")).toBeVisible();
    expect(host.requests("/studio/scope")[0].body).toEqual({
      tenantId: manyWorkspaces[7].tenant_id,
      projectId: manyWorkspaces[7].project_id,
      environmentId: manyWorkspaces[7].environment_id,
    });
  });

  test("no workspaces explains access with details to copy", async ({
    page,
    context,
  }) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await platformHost(page, {
      platforms: [acmePlatform({ workspace: null, workspaces: [] })],
      active: "Acme",
    });
    await page.locator(".platform-indicator").click();
    await page.getByRole("button", { name: "Switch workspace" }).click();
    await expect(
      page.getByRole("heading", {
        name: "Your account doesn't have access to a workspace yet",
      }),
    ).toBeVisible();
    const details = page.getByLabel("Details for your administrator");
    await expect(details).toHaveValue(/Account: jane@acme\.example/);
    await expect(details).toHaveValue(/Subject: 00u1-jane/);
    await page.getByRole("button", { name: "Copy details" }).click();
    expect(await page.evaluate(() => navigator.clipboard.readText())).toContain(
      "Platform: Acme (https://weave.acme.example)",
    );
    await expect(
      page.getByRole("button", { name: "Start working" }),
    ).toHaveCount(0);
  });

  test("an account the platform doesn't know gets the linking details", async ({
    page,
  }) => {
    await platformHost(page, {
      platforms: [acmePlatform()],
      active: "Acme",
      test: () => ({
        status: 401,
        json: {
          status: 401,
          code: "WV-AUTH-NOT-LINKED",
          message:
            "You signed in, but this platform does not know your account yet.",
          account: {
            provider_id: "acme",
            issuer: acmeOption.issuer,
            subject: "00u9-new",
            display_name: "new.person@acme.example",
          },
        },
      }),
    });
    const banner = page.locator(".platform-banner");
    await expect(banner).toContainText(
      "You signed in, but Acme doesn't recognize your account yet.",
    );
    await expect(page.locator(".platform-indicator")).toHaveAccessibleName(
      /Account not recognized/,
    );
    await banner.getByRole("button", { name: "See details" }).click();
    await expect(
      page.getByRole("heading", {
        name: "You signed in, but this platform doesn't recognize your account yet",
      }),
    ).toBeVisible();
    await expect(page.getByLabel("Details for your administrator")).toHaveValue(
      /Subject: 00u9-new/,
    );
    await expect(
      page.getByRole("button", { name: "Sign in with a different account" }),
    ).toBeVisible();
    await expect(wizard(page)).not.toContainText("WV-AUTH-NOT-LINKED");
  });
});

test.describe("startup and saved platforms", () => {
  test("startup verifies silently and reports an expired session without blocking", async ({
    page,
  }) => {
    const host = await platformHost(page, {
      platforms: [acmePlatform({ signedIn: false })],
      active: "Acme",
    });
    const banner = page.locator(".platform-banner");
    await expect(banner).toContainText(
      "Your session for Acme expired. Sign in again",
    );
    await expect(page.locator("weave-home-dashboard")).toBeVisible();
    await page
      .getByRole("button", { name: "New workflow", exact: true })
      .click();
    await expect(page.locator(".canvas")).toBeVisible();
    await expect(page.locator(".platform-indicator")).toHaveAccessibleName(
      "Platform: Acme · Session expired · Workspace Payments / Production",
    );
    await banner.getByRole("button", { name: "Sign in again" }).click();
    await expect(heading(page)).toHaveText("Sign in to Acme");
    expect(host.requests("/studio/connection/test")).toHaveLength(1);
  });

  test("startup with a valid session loads identity and the workspace", async ({
    page,
  }) => {
    const host = await platformHost(page, {
      platforms: [acmePlatform()],
      active: "Acme",
    });
    await expect(page.locator(".platform-indicator")).toHaveAccessibleName(
      "Platform: Acme · Signed in as jane@acme.example · Workspace Payments / Production",
    );
    await expect(page.locator(".platform-banner")).toHaveCount(0);
    await expect(page.locator(".needs-you")).toBeVisible();
    expect(host.requests("/studio/connection/test")).toHaveLength(1);
  });

  for (const [kind, code, banner, state] of [
    [
      "an unreachable platform",
      "WV-AUTH-OFFLINE",
      "Studio couldn't reach Acme. You can keep working locally.",
      "Can't reach the platform",
    ],
    [
      "a locked credential store",
      "WV-AUTH-STORE",
      "Studio can't use this computer's credential store.",
      "Credential store unavailable",
    ],
  ])
    test(`startup reports ${kind} without blocking and checks again`, async ({
      page,
    }) => {
      let failures = 1;
      const host = await platformHost(page, {
        platforms: [acmePlatform()],
        active: "Acme",
        test: () =>
          failures-- > 0
            ? {
                status: 503,
                json: { status: 503, code, message: "Not available." },
              }
            : undefined,
      });
      const notice = page.locator(".platform-banner");
      await expect(notice).toContainText(banner);
      await expect(notice).not.toContainText("WV-");
      await expect(page.locator(".platform-indicator")).toHaveAccessibleName(
        `Platform: Acme · ${state} · Workspace Payments / Production`,
      );
      await expect(page.locator("weave-home-dashboard")).toBeVisible();
      await notice.getByRole("button", { name: "Try again" }).click();
      await expect(page.locator(".platform-banner")).toHaveCount(0);
      await expect(page.locator(".platform-indicator")).toHaveAccessibleName(
        "Platform: Acme · Signed in as jane@acme.example · Workspace Payments / Production",
      );
      expect(host.requests("/studio/connection/test")).toHaveLength(2);
    });

  test("a workspace the account lost is reported and chosen again", async ({
    page,
  }) => {
    let revoked = false;
    const host = await platformHost(page, {
      platforms: [acmePlatform()],
      active: "Acme",
      test: (host) => {
        if (revoked) return undefined;
        revoked = true;
        // The host forgets a saved workspace the account can no longer use.
        host.current!.workspace = null;
        host.current!.workspaces = acme.slice(1);
        return {
          json: {
            session: host.session(),
            identity: host.identity(),
            workspaces: acme.slice(1),
            truncated: false,
            workspace_revoked: true,
          },
        };
      },
    });
    const notice = page.locator(".platform-banner");
    await expect(notice).toContainText(
      "The workspace you used on Acme is no longer available to your account.",
    );
    await expect(page.locator(".platform-indicator")).toHaveAccessibleName(
      "Platform: Acme · Signed in as jane@acme.example · No workspace chosen",
    );
    await notice.getByRole("button", { name: "Choose workspace" }).click();
    await expect(heading(page)).toHaveText("Choose a workspace");
    await expect(page.getByRole("radio", { checked: true })).toHaveCount(0);
    await page
      .getByRole("radio", { name: "Acme / Billing / Production" })
      .check();
    await page.getByRole("button", { name: "Start working" }).click();
    await expect(page.locator(".platform-indicator")).toHaveAccessibleName(
      "Platform: Acme · Signed in as jane@acme.example · Workspace Billing / Production",
    );
    expect(host.requests("/studio/scope")).toHaveLength(1);
  });

  test("an expired access token the host can renew never asks to sign in again", async ({
    page,
  }) => {
    const host = await platformHost(page, {
      platforms: [
        acmePlatform({ signedIn: false, refreshable: true }),
        globexPlatform({ signedIn: false, refreshable: true }),
      ],
      active: "Acme",
    });
    await expect(page.locator(".platform-indicator")).toHaveAccessibleName(
      "Platform: Acme · Signed in as jane@acme.example · Workspace Payments / Production",
    );
    await expect(page.locator(".platform-banner")).toHaveCount(0);
    // The renewed session is reflected everywhere the saved state shows.
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await page.getByRole("button", { name: "Add platform" }).click();
    await expect(heading(page)).toHaveText("Connect to a platform");
    await expect(
      page.getByRole("button", { name: "Use saved platform Acme" }),
    ).toContainText("Signed in");
    // Another platform with a renewable session goes straight to workspaces.
    await page
      .getByRole("button", { name: "Use saved platform Globex" })
      .click();
    await expect(heading(page)).toHaveText("Choose a workspace");
    await expect(
      page.getByRole("radio", { name: "Globex / Core / Development" }),
    ).toBeVisible();
    expect(host.requests("/studio/connection/login/start")).toHaveLength(0);
  });

  test("Use in Settings renews an expired access token instead of signing in", async ({
    page,
  }) => {
    const host = await platformHost(page, {
      platforms: [
        acmePlatform(),
        globexPlatform({ signedIn: false, refreshable: true }),
      ],
      active: "Acme",
    });
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await page
      .locator(".platform-row")
      .filter({ hasText: "Globex" })
      .getByRole("button", { name: "Switch to this platform" })
      .click();
    await expect(heading(page)).toHaveText("Choose a workspace");
    expect(host.requests("/studio/connection/login/start")).toHaveLength(0);
  });

  test("an account without a shared name still reads as signed in", async ({
    page,
  }) => {
    await platformHost(page, {
      platforms: [acmePlatform({ account: null })],
      active: "Acme",
    });
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    const row = page
      .getByRole("list", { name: "Saved platforms" })
      .locator("li")
      .filter({ hasText: "Acme" });
    await expect(row).toContainText("Signed in");
    await expect(row).not.toContainText("Not signed in yet");
  });

  test("the platform menu offers workspace, account and sign-out actions", async ({
    page,
  }) => {
    await platformHost(page, { platforms: [acmePlatform()], active: "Acme" });
    const indicator = page.locator(".platform-indicator");
    await expect(indicator).toHaveAttribute("aria-expanded", "false");
    await indicator.click();
    await expect(indicator).toHaveAttribute("aria-expanded", "true");
    const menu = page.locator("#platform-menu");
    await expect(menu.getByRole("button")).toHaveText([
      "Switch workspace",
      "Switch account",
      "Sign out",
      "Platform settings",
    ]);
    await expect(menu.getByRole("button").first()).toBeFocused();
    await page.keyboard.press("ArrowDown");
    await expect(
      menu.getByRole("button", { name: "Switch account" }),
    ).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(menu).toHaveCount(0);
    await expect(indicator).toBeFocused();
    await indicator.click();
    await menu.getByRole("button", { name: "Platform settings" }).click();
    await expect(
      page.getByRole("heading", { name: "Platforms" }),
    ).toBeVisible();
  });

  test("Settings lists saved platforms and runs each action through the host", async ({
    page,
  }) => {
    const host = await platformHost(page, {
      platforms: [acmePlatform(), globexPlatform({ signedIn: false })],
      active: "Acme",
    });
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    const list = page.getByRole("list", { name: "Saved platforms" });
    const acmeRow = list.locator("li").filter({ hasText: "Acme" });
    const globexRow = list.locator("li").filter({ hasText: "Globex" });
    await expect(acmeRow).toContainText("In use");
    await expect(acmeRow).toContainText("Signed in");
    await expect(acmeRow).toContainText("jane@acme.example");
    await expect(acmeRow).toContainText("Acme / Payments / Production");
    await expect(globexRow).toContainText("Not signed in");
    await expect(
      page.getByRole("heading", { name: "Current platform" }),
    ).toHaveCount(0);
    await expect(
      page.getByRole("heading", { name: "API profile" }),
    ).toHaveCount(0);
    await expect(
      page.getByRole("heading", { name: "Authorized workspaces" }),
    ).toHaveCount(0);

    // Switch account: a browser sign-in that asks for the account again.
    await page.getByRole("button", { name: "Switch account on Acme" }).click();
    await expect(heading(page)).toHaveText("Sign in to Acme");
    await expect(wizard(page)).toContainText(
      "Choose a different account on the sign-in page.",
    );
    await page
      .getByRole("button", { name: "Sign in with your browser" })
      .click();
    await expect
      .poll(() => host.requests("/studio/connection/login/start").length)
      .toBe(1);
    expect(host.requests("/studio/connection/login/start")[0].body).toEqual({
      flow: "browser",
      switch_account: true,
    });
    await page.getByRole("button", { name: "Cancel sign-in" }).click();
    await page.getByRole("button", { name: "Settings", exact: true }).click();

    // Sign out asks first.
    await page.getByRole("button", { name: "Sign out of Acme" }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toContainText("Sign out of Acme?");
    await dialog.getByRole("button", { name: "Cancel" }).click();
    expect(host.requests("/studio/connection/logout")).toHaveLength(0);
    await page.getByRole("button", { name: "Sign out of Acme" }).click();
    await dialog.getByRole("button", { name: "Sign out" }).click();
    await expect(acmeRow).toContainText("Signed out");
    expect(host.requests("/studio/connection/logout")[0].body).toEqual({
      revoke: true,
    });
    await expect(
      page.getByRole("button", { name: "Sign in to Acme" }),
    ).toBeVisible();

    // Use another platform: it is signed out, so sign-in follows.
    await globexRow
      .getByRole("button", { name: "Switch to this platform" })
      .click();
    await expect(heading(page)).toHaveText("Sign in to Globex");
    expect(host.requests("/studio/connection/activate")[0].body).toEqual({
      name: "Globex",
    });
    await page.getByRole("button", { name: "Cancel" }).click();

    // Remove asks first and names what happens.
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    // Removing lives in the row's "⋯" menu.
    await page.getByRole("button", { name: "More actions for Acme" }).click();
    await page.getByRole("menuitem", { name: "Remove Acme" }).click();
    await expect(dialog).toContainText("Remove Acme?");
    await dialog.getByRole("button", { name: "Remove platform" }).click();
    await expect(list.locator("li")).toHaveCount(1);
    expect(host.requests("/studio/connection/remove")[0].body).toEqual({
      name: "Acme",
      sign_out: true,
    });

    // Work locally clears the active platform but keeps saved ones.
    await page
      .locator("weave-platform-list")
      .getByRole("button", { name: "Work locally" })
      .click();
    await expect(page.locator(".platform-indicator")).toHaveAccessibleName(
      "Platform: Local authoring",
    );
    expect(host.requests("/studio/connection/disconnect")).toHaveLength(1);
    await expect(list.locator("li")).toHaveCount(1);
    await expect(
      page
        .locator("weave-platform-list")
        .getByRole("button", { name: "Connect to a platform" }),
    ).toBeVisible();
    expect(host.csrfViolations).toEqual([]);
  });

  test("switching platforms keeps the open workflow and asks only for unsaved platform work", async ({
    page,
  }) => {
    const host = await platformHost(page, {
      platforms: [acmePlatform(), globexPlatform()],
      active: "Acme",
    });
    await page.route("**/drafts/*", (route) =>
      route.fulfill({
        json: {
          id: "draft",
          revision: 1,
          document: route.request().postDataJSON()?.document,
        },
      }),
    );
    await expect(page.locator(".platform-indicator")).toHaveAccessibleName(
      /Signed in/,
    );
    await page
      .getByRole("button", { name: "New workflow", exact: true })
      .click();
    await page
      .locator(".palette-step")
      .filter({ hasText: "Transform" })
      .click();
    await page.getByRole("button", { name: "Save draft" }).click();
    await expect(page.locator(".editor-identity .status-chip")).toHaveText(
      /Draft saved \d{2}:\d{2}/,
    );
    await page
      .locator(".palette-step")
      .filter({ hasText: "Transform" })
      .click();
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await page.getByRole("button", { name: "Leave designer" }).click();
    await page.getByRole("button", { name: "Add platform" }).click();
    await page
      .getByRole("button", { name: "Use saved platform Globex" })
      .click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toContainText(
      "The open workflow has changes that aren't saved to Acme.",
    );
    await dialog.getByRole("button", { name: "Stay here" }).click();
    expect(host.requests("/studio/connection/activate")).toHaveLength(0);
    await page
      .getByRole("button", { name: "Use saved platform Globex" })
      .click();
    await dialog.getByRole("button", { name: "Switch platform" }).click();
    await expect(heading(page)).toHaveText("Choose a workspace");
    await page.getByRole("radio", { name: "Globex / Core / Test" }).check();
    await page.getByRole("button", { name: "Start working" }).click();
    // Home leads back to the workflow still open in this window.
    await page
      .getByRole("button", { name: /^Continue editing untitled-workflow/ })
      .click();
    await expect(page.locator('[data-step="transform-1"]')).toBeVisible();
    await expect(page.locator('[data-step="transform-2"]')).toBeVisible();
    await expect(page.locator(".editor-identity .status-chip")).toHaveText(
      "Unsaved",
    );
    await expect(page.locator(".platform-indicator")).toHaveAccessibleName(
      "Platform: Globex · Signed in · Workspace Core / Test",
    );
  });

  test("Studio started from a connection file explains saved platforms", async ({
    page,
  }) => {
    await platformHost(page, {
      store: false,
      platforms: [acmePlatform()],
      active: "Acme",
    });
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await expect(page.locator("weave-platform-list")).toContainText(
      "started with a connection file",
    );
    await expect(
      page.getByRole("button", { name: "Add platform" }),
    ).toHaveCount(0);
    await expect(
      page.getByRole("button", { name: "More actions for Acme" }),
    ).toHaveCount(0);
  });
});

test("the whole wizard completes with the keyboard only", async ({ page }) => {
  const host = await platformHost(page, {
    discover: discovery,
    firstRun: true,
  });
  await expect(heading(page)).toHaveText("How do you want to work?");
  await page.keyboard.press("Tab");
  await page.keyboard.press("Tab");
  await expect(
    page.getByRole("button", { name: "Connect to a platform" }),
  ).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(heading(page)).toHaveText("Connect to a platform");
  await page.keyboard.press("Tab");
  await expect(page.getByLabel("Server address")).toBeFocused();
  await page.keyboard.type("weave.acme.example");
  await page.keyboard.press("Enter");
  await expect(heading(page)).toHaveText("Review and trust");
  await expect(heading(page)).toBeFocused();
  // Name, then the trust checkbox; Enter submits from either.
  await page.keyboard.press("Tab");
  await expect(page.getByLabel("Name", { exact: true })).toBeFocused();
  await page.keyboard.press("Tab");
  const trust = page.getByLabel("I trust this server and identity provider");
  await expect(trust).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator("#wizard-trust-error")).toBeVisible();
  await expect(trust).toBeFocused();
  await page.keyboard.press("Space");
  await expect(trust).toBeChecked();
  await page.keyboard.press("Shift+Tab");
  await page.keyboard.press("Enter");
  await expect(heading(page)).toHaveText("Sign in to Acme Weave");
  await page.keyboard.press("Tab");
  await expect(
    page.getByRole("button", { name: "Sign in with your browser" }),
  ).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(
    page.getByRole("textbox", { name: "Sign-in address" }),
  ).toBeVisible({
    timeout: login,
  });
  host.finish("authenticated");
  await expect(heading(page)).toHaveText("Choose a workspace", {
    timeout: login,
  });
  await page.keyboard.press("Tab");
  const first = page.getByRole("radio", {
    name: "Acme / Payments / Production",
  });
  await expect(first).toBeFocused();
  await page.keyboard.press("ArrowDown");
  await expect(
    page.getByRole("radio", { name: "Acme / Payments / Staging" }),
  ).toBeChecked();
  await page.keyboard.press("Enter");
  await expect(page.locator("weave-home-dashboard")).toBeVisible();
  expect(host.requests("/studio/scope")[0].body).toEqual({
    tenantId: acme[1].tenant_id,
    projectId: acme[1].project_id,
    environmentId: acme[1].environment_id,
  });
});

for (const size of [
  { width: 360, height: 740 },
  { width: 600, height: 500 },
  { width: 1024, height: 768 },
  { width: 1440, height: 900 },
])
  test(`every step fits ${size.width}x${size.height} with labelled controls`, async ({
    page,
  }) => {
    const tag = `${size.width}x${size.height}`;
    await page.setViewportSize(size);
    const shot = async (name: string) => {
      await expectNoOverflow(page, `${name} ${tag}`);
      await expectLabelledControls(page);
      await page.screenshot({
        path: `test-results/connection/${name}-${tag}.png`,
        fullPage: true,
      });
    };
    const host = await platformHost(page, {
      firstRun: true,
      discover: {
        ...discovery,
        "down.example": {
          status: 502,
          json: { status: 502, code: "WV-CONNECT-UNREACHABLE", message: "x" },
        },
      },
    });
    await expect(heading(page)).toHaveText("How do you want to work?");
    await shot("1-choice");
    await page.getByRole("button", { name: "Connect to a platform" }).click();
    await page.getByLabel("Server address").fill("down.example");
    await page.getByRole("button", { name: "Continue" }).click();
    await expect(page.locator("#wizard-server-problem")).toBeVisible();
    await shot("2-server-error");
    await discoverAcme(page);
    await page.getByText("Advanced", { exact: true }).click();
    await shot("3-review");
    await page.getByLabel("I trust this server and identity provider").check();
    await page.getByRole("button", { name: "Continue" }).click();
    await expect(heading(page)).toHaveText("Sign in to Acme Weave");
    await shot("4-sign-in");
    await page
      .getByRole("button", { name: "Sign in with your browser" })
      .click();
    await expect(
      page.getByRole("textbox", { name: "Sign-in address" }),
    ).toBeVisible({
      timeout: login,
    });
    await shot("5-sign-in-browser");
    await page.getByRole("button", { name: "Cancel sign-in" }).click();
    // Canceling is not a problem: a neutral note, not an alert.
    await expect(page.locator("#wizard-sign-in-problem")).toHaveAttribute(
      "role",
      "status",
    );
    await expect(page.locator("#wizard-sign-in-problem")).toContainText(
      "Sign-in canceled.",
    );
    await expect(page.locator("#wizard-sign-in-problem")).toContainText(
      "Nothing changed.",
    );
    await shot("6-sign-in-canceled");
    await page.getByRole("button", { name: "Back" }).click();
    await page.getByRole("button", { name: "Continue" }).click();
    await page.getByRole("button", { name: "Use a code instead" }).click();
    await expect(
      page.getByRole("status", { name: "Sign-in code" }),
    ).toBeVisible({ timeout: login });
    await shot("7-sign-in-code");
    host.finish("authenticated");
    await expect(heading(page)).toHaveText("Choose a workspace", {
      timeout: login,
    });
    await page
      .getByRole("radio", { name: "Acme / Billing / Production" })
      .check();
    await shot("8-workspace");
    await page.getByRole("button", { name: "Start working" }).click();
    await expect(page.locator("weave-home-dashboard")).toBeVisible();
    await page.locator(".platform-indicator").click();
    await expect(page.locator("#platform-menu")).toBeVisible();
    await shot("9-platform-menu");
    await page
      .locator("#platform-menu")
      .getByRole("button", { name: "Platform settings" })
      .click();
    await expect(
      page.getByRole("heading", { name: "Platforms" }),
    ).toBeVisible();
    await shot("10-settings-platforms");
  });

test.describe("sign-in methods the administrator allows", () => {
  const signInAgain = async (page: Page) => {
    await page
      .locator(".platform-banner")
      .getByRole("button", { name: "Sign in again" })
      .click();
    await expect(heading(page)).toHaveText("Sign in to Acme");
  };

  test("a saved platform without code sign-in offers only the browser", async ({
    page,
  }) => {
    await platformHost(page, {
      platforms: [acmePlatform({ signedIn: false, flows: ["browser"] })],
      active: "Acme",
    });
    await signInAgain(page);
    await expect(
      page.getByRole("button", { name: "Sign in with your browser" }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Use a code instead" }),
    ).toHaveCount(0);
  });

  test("a reviewed option without code sign-in offers only the browser", async ({
    page,
  }) => {
    await platformHost(page, {
      discover: {
        "weave.acme.example": {
          json: {
            ...acmeDiscovery,
            sign_in: [{ ...acmeOption, flows: ["browser"] }],
          },
        },
      },
    });
    await openConnect(page);
    await discoverAcme(page);
    await expect(wizard(page)).toContainText("Your web browser");
    await trustAndContinue(page);
    await expect(
      page.getByRole("button", { name: "Sign in with your browser" }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Use a code instead" }),
    ).toHaveCount(0);
  });

  test("a platform that allows only code sign-in never offers the browser", async ({
    page,
  }) => {
    const host = await platformHost(page, {
      platforms: [acmePlatform({ signedIn: false, flows: ["device"] })],
      active: "Acme",
    });
    await signInAgain(page);
    await expect(
      page.getByRole("button", { name: "Sign in with your browser" }),
    ).toHaveCount(0);
    await expect(
      page.getByRole("button", { name: "Use a code instead" }),
    ).toHaveCount(0);
    await expect(wizard(page)).toContainText(
      "Your administrator allows sign-in with a code only.",
    );
    await page.getByRole("button", { name: "Sign in with a code" }).click();
    await expect(page.getByRole("status", { name: "Sign-in code" })).toHaveText(
      "WDJB-MJHT",
      { timeout: login },
    );
    expect(host.requests("/studio/connection/login/start")[0].body).toEqual({
      flow: "device",
      switch_account: false,
    });
  });

  test("when no allowed method is left Studio says so once, without Try again", async ({
    page,
  }) => {
    await platformHost(page, {
      platforms: [acmePlatform({ signedIn: false, flows: ["device"] })],
      active: "Acme",
      enforcedFlows: ["browser"],
    });
    await signInAgain(page);
    await page.getByRole("button", { name: "Sign in with a code" }).click();
    const alert = page.locator("#wizard-sign-in-problem");
    await expect(alert.locator("strong")).toHaveText(
      "Your administrator doesn't allow this sign-in method",
    );
    await expect(alert).toContainText(
      "Acme allows no other sign-in method Studio can use. Ask your administrator to check the platform's sign-in settings.",
    );
    await expect(wizard(page)).not.toContainText("the other method");
    await expect(wizard(page)).toContainText("allows no other sign-in method", {
      useInnerText: true,
    });
    expect(
      (await wizard(page).innerText()).split("allows no other sign-in method")
        .length,
    ).toBe(2);
    await expect(page.getByRole("button", { name: "Try again" })).toHaveCount(
      0,
    );
  });

  test("switching account with a code shows the host's guidance", async ({
    page,
  }) => {
    const host = await platformHost(page, {
      platforms: [acmePlatform({ flows: ["device"] })],
      active: "Acme",
    });
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await page.getByRole("button", { name: "Switch account on Acme" }).click();
    await expect(heading(page)).toHaveText("Sign in to Acme");
    await page.getByRole("button", { name: "Sign in with a code" }).click();
    await expect(page.getByRole("status", { name: "Sign-in code" })).toHaveText(
      "WDJB-MJHT",
      { timeout: login },
    );
    await expect(wizard(page)).toContainText(
      "choose the account you want to use (sign out of the other account first if the page signs you in automatically).",
    );
    expect(host.requests("/studio/connection/login/start")[0].body).toEqual({
      flow: "device",
      switch_account: true,
    });
  });

  test("a method the host refuses is explained and Try again uses another", async ({
    page,
  }) => {
    const host = await platformHost(page, {
      platforms: [acmePlatform({ signedIn: false })],
      active: "Acme",
      enforcedFlows: ["browser"],
    });
    await signInAgain(page);
    await page.getByRole("button", { name: "Use a code instead" }).click();
    const alert = page.locator("#wizard-sign-in-problem");
    await expect(alert.locator("strong")).toHaveText(
      "Your administrator doesn't allow this sign-in method",
    );
    await expect(alert.locator("strong")).not.toContainText("WV-");
    await expect(alert.locator(".support-code")).toHaveText(
      "Support code: WV-AUTH-FLOW",
    );
    await page.getByRole("button", { name: "Try again" }).click();
    await expect(
      page.getByRole("textbox", { name: "Sign-in address" }),
    ).toBeVisible({ timeout: login });
    expect(
      host.requests("/studio/connection/login/start").map((c) => c.body),
    ).toEqual([
      { flow: "device", switch_account: false },
      { flow: "browser", switch_account: false },
    ]);
    // Trying again after a cancel keeps to the method the host accepts.
    await page.getByRole("button", { name: "Cancel sign-in" }).click();
    await page.getByRole("button", { name: "Try again" }).click();
    await expect
      .poll(() => host.requests("/studio/connection/login/start").length)
      .toBe(3);
    expect(host.requests("/studio/connection/login/start")[2].body).toEqual({
      flow: "browser",
      switch_account: false,
    });
  });
});

test.describe("a platform saved during this connection", () => {
  test("going back keeps the identity provider the platform was saved with", async ({
    page,
  }) => {
    const partners = {
      ...partnerOption,
      checks: { browser: true, device: true },
      problem: null,
    };
    const host = await platformHost(page, {
      discover: {
        "weave.acme.example": {
          json: { ...acmeDiscovery, sign_in: [acmeOption, partners] },
        },
        "https://weave.acme.example": {
          json: { ...acmeDiscovery, sign_in: [acmeOption, partners] },
        },
      },
    });
    await openConnect(page);
    await discoverAcme(page);
    await page.getByRole("radio", { name: /Partner accounts/ }).check();
    await trustAndContinue(page);
    expect(host.requests("/studio/connection/profiles")[0].body).toMatchObject({
      provider_id: "partners",
    });
    await page.getByRole("button", { name: "Back" }).click();
    await page.getByRole("button", { name: "Back" }).click();
    await expect(heading(page)).toHaveText("Connect to a platform");
    await page.getByRole("button", { name: "Continue" }).click();
    await expect(heading(page)).toHaveText("Review and trust");
    await expect(wizard(page)).toContainText("Saved as Acme Weave.");
    const saved = page.getByRole("radio", { name: /Partner accounts/ });
    await expect(saved).toBeChecked();
    await expect(saved).toBeDisabled();
    await expect(
      page.getByRole("radio", { name: /Acme \(Microsoft Entra ID\)/ }),
    ).not.toBeChecked();
    await page.getByRole("button", { name: "Continue" }).click();
    await expect(heading(page)).toHaveText("Sign in to Acme Weave");
    await expect(wizard(page)).toContainText("with Partner accounts");
    expect(host.requests("/studio/connection/profiles")).toHaveLength(1);
  });

  test("removing a platform that was signed in says what happened to the sign-in", async ({
    page,
  }) => {
    const host = await platformHost(page, {
      discover: {
        ...discovery,
        "https://weave.acme.example": { json: acmeDiscovery },
      },
      revocation: "unconfirmed",
    });
    await openConnect(page);
    await discoverAcme(page);
    await trustAndContinue(page);
    await page
      .getByRole("button", { name: "Sign in with your browser" })
      .click();
    await expect(
      page.getByRole("textbox", { name: "Sign-in address" }),
    ).toBeVisible({ timeout: login });
    host.finish("authenticated");
    await expect(heading(page)).toHaveText("Choose a workspace", {
      timeout: login,
    });
    await page.getByRole("button", { name: "Back" }).click();
    await page.getByRole("button", { name: "Back" }).click();
    await expect(heading(page)).toHaveText("Review and trust");
    await page.getByRole("button", { name: "Remove and start over" }).click();
    await expect(heading(page)).toHaveText("Connect to a platform");
    // The wizard says it at once; the shell's banner is hidden on this page.
    const note = wizard(page).getByRole("status");
    await expect(note).toHaveText(
      "Removed Acme Weave. Signed out on this computer. Your identity provider did not confirm the sign-out.",
    );
    await expect(note).not.toContainText("WV-");
    await page.getByRole("button", { name: "Continue" }).click();
    await expect(heading(page)).toHaveText("Review and trust");
    await page.getByRole("button", { name: "Back" }).click();
    await expect(heading(page)).toHaveText("Connect to a platform");
    await expect(wizard(page).getByRole("status")).toHaveCount(0);
  });

  test("a removal that leaves the sign-in on this computer names the platform", async ({
    page,
  }) => {
    await platformHost(page, {
      discover: {
        ...discovery,
        "https://weave.acme.example": { json: acmeDiscovery },
      },
      keepCredentialsOnRemove: true,
    });
    await openConnect(page);
    await discoverAcme(page);
    await trustAndContinue(page);
    await page.getByRole("button", { name: "Back" }).click();
    await page.getByRole("button", { name: "Remove and start over" }).click();
    await expect(heading(page)).toHaveText("Connect to a platform");
    await expect(wizard(page).getByRole("status")).toHaveText(
      "Removed Acme Weave, but Studio couldn't remove its sign-in from this computer's credential store.",
    );
    await page.getByRole("button", { name: "Home", exact: true }).click();
    await expect(page.locator(".platform-banner")).toContainText(
      "Studio removed Acme Weave, but couldn't remove its sign-in from this computer's credential store.",
    );
  });

  test("a platform saved from a connection file is not saved twice either", async ({
    page,
  }) => {
    const host = await platformHost(page);
    await openConnect(page);
    await page.getByText("Advanced", { exact: true }).click();
    await page.getByRole("button", { name: "Use a connection file" }).click();
    await expect(heading(page)).toHaveText("Use a connection file");
    await page.getByLabel("Connection file", { exact: true }).setInputFiles({
      name: "acme-operator.json",
      mimeType: "application/json",
      buffer: Buffer.from(
        JSON.stringify({
          provider_id: "acme",
          issuer: "https://login.acme.example/tenant/v2.0",
          client_id: "weave-cli",
          target: "https://weave.acme.example",
        }),
      ),
    });
    await trustAndContinue(page);
    await page.getByRole("button", { name: "Back" }).click();
    await expect(heading(page)).toHaveText("Use a connection file");
    await expect(page.getByLabel("Name", { exact: true })).toHaveAttribute(
      "readonly",
      "",
    );
    await expect(wizard(page)).toContainText("Saved as acme-operator.");
    await page.getByRole("button", { name: "Continue" }).click();
    await expect(heading(page)).toHaveText(/^Sign in to /);
    expect(host.requests("/studio/connection/configure")).toHaveLength(1);
    await page.getByRole("button", { name: "Back" }).click();
    await page.getByRole("button", { name: "Remove and start over" }).click();
    await expect(heading(page)).toHaveText("Connect to a platform");
    expect(host.requests("/studio/connection/remove")[0].body).toEqual({
      name: "acme-operator",
      sign_out: true,
    });
    await expect(page.getByLabel("Server address")).toHaveValue(
      "https://weave.acme.example",
    );
    await expect(wizard(page).getByRole("status")).toHaveText(
      "Removed acme-operator.",
    );
  });

  test("going back to review shows the saved name instead of saving again", async ({
    page,
  }) => {
    const host = await platformHost(page, {
      discover: {
        ...discovery,
        "https://weave.acme.example": { json: acmeDiscovery },
      },
    });
    await openConnect(page);
    await discoverAcme(page);
    await trustAndContinue(page);
    await page.getByRole("button", { name: "Back" }).click();
    await expect(heading(page)).toHaveText("Review and trust");
    const name = page.getByLabel("Name", { exact: true });
    await expect(name).toHaveValue("Acme Weave");
    await expect(name).toHaveAttribute("readonly", "");
    await expect(wizard(page)).toContainText("Saved as Acme Weave.");
    await expect(
      page.getByLabel("I trust this server and identity provider"),
    ).toHaveCount(0);
    await page.getByRole("button", { name: "Continue" }).click();
    await expect(heading(page)).toHaveText("Sign in to Acme Weave");
    expect(host.requests("/studio/connection/profiles")).toHaveLength(1);
    await expectLabelledControls(page);

    // Remove and start over: the saved platform goes, the address stays.
    await page.getByRole("button", { name: "Back" }).click();
    await page.getByRole("button", { name: "Remove and start over" }).click();
    await expect(heading(page)).toHaveText("Connect to a platform");
    expect(host.requests("/studio/connection/remove")[0].body).toEqual({
      name: "Acme Weave",
      sign_out: true,
    });
    await expect(page.getByLabel("Server address")).toHaveValue(
      "https://weave.acme.example",
    );
    await expect(page.locator(".platform-indicator")).toHaveAccessibleName(
      "Platform: Local authoring",
    );
    await page.getByRole("button", { name: "Continue" }).click();
    await expect(heading(page)).toHaveText("Review and trust");
    await page.getByLabel("Name", { exact: true }).fill("Acme production");
    await trustAndContinue(page);
    await expect(heading(page)).toHaveText("Sign in to Acme production");
    expect(host.platforms.map((p) => p.name)).toEqual(["Acme production"]);
    expect(host.csrfViolations).toEqual([]);
  });
});

test.describe("a sign-in left running", () => {
  async function leaveWaiting(page: Page) {
    const host = await platformHost(page, {
      platforms: [acmePlatform({ signedIn: false })],
      active: "Acme",
    });
    await page
      .locator(".platform-banner")
      .getByRole("button", { name: "Sign in again" })
      .click();
    await page
      .getByRole("button", { name: "Sign in with your browser" })
      .click();
    await expect(
      page.getByRole("textbox", { name: "Sign-in address" }),
    ).toBeVisible({ timeout: login });
    await page.getByRole("button", { name: "Home", exact: true }).click();
    await expect(wizard(page)).toHaveCount(0);
    await expect(page.locator(".platform-banner")).toContainText(
      "Studio is waiting for you to finish signing in to Acme.",
    );
    await expect(page.locator(".platform-indicator")).toHaveAccessibleName(
      "Platform: Acme · Signing in · Workspace Payments / Production",
    );
    return host;
  }

  test("Studio keeps checking it and shows the result once it finishes", async ({
    page,
  }) => {
    const host = await leaveWaiting(page);
    const polls = host.calls.filter(
      (c) => c.path === "/studio/connection/login/login-1",
    ).length;
    await expect
      .poll(
        () =>
          host.calls.filter(
            (c) => c.path === "/studio/connection/login/login-1",
          ).length,
        { timeout: login },
      )
      .toBeGreaterThan(polls);
    host.finish("authenticated");
    await expect(page.locator(".platform-indicator")).toHaveAccessibleName(
      "Platform: Acme · Signed in as jane@acme.example · Workspace Payments / Production",
      { timeout: login },
    );
    await expect(page.locator(".platform-banner")).toHaveCount(0);
    expect(host.requests("/studio/connection/login/start")).toHaveLength(1);
  });

  test("a sign-in that fails after the wizard closed is reported", async ({
    page,
  }) => {
    const host = await leaveWaiting(page);
    host.finish("failed", "WV-AUTH-DENIED");
    const banner = page.locator(".platform-banner");
    await expect(banner).toContainText("Sign-in was declined.", {
      timeout: login,
    });
    await expect(banner).not.toContainText("WV-");
    await expect(page.locator(".platform-indicator")).toHaveAccessibleName(
      /Signed out/,
    );
    await banner.getByRole("button", { name: "Sign in again" }).click();
    await expect(heading(page)).toHaveText("Sign in to Acme");
    await expect(
      page.getByRole("button", { name: "Sign in with your browser" }),
    ).toBeVisible();
  });

  test("Show sign-in reopens the same sign-in without starting another", async ({
    page,
  }) => {
    const host = await leaveWaiting(page);
    await page
      .locator(".platform-banner")
      .getByRole("button", { name: "Show sign-in" })
      .click();
    await expect(heading(page)).toHaveText("Sign in to Acme");
    await expect(
      page.getByRole("textbox", { name: "Sign-in address" }),
    ).toBeVisible();
    host.finish("authenticated");
    await expect(heading(page)).toHaveText("Choose a workspace", {
      timeout: login,
    });
    expect(host.requests("/studio/connection/login/start")).toHaveLength(1);
  });

  test("a sign-in still starting when the wizard closes is followed too", async ({
    page,
  }) => {
    const host = await platformHost(page, {
      platforms: [acmePlatform({ signedIn: false })],
      active: "Acme",
    });
    let release = () => {};
    const answered = new Promise<void>((done) => (release = done));
    // The host takes a moment to start the sign-in.
    await page.route("**/studio/connection/login/start", async (route) => {
      await answered;
      await route.fallback();
    });
    await page
      .locator(".platform-banner")
      .getByRole("button", { name: "Sign in again" })
      .click();
    await page
      .getByRole("button", { name: "Sign in with your browser" })
      .click();
    await expect(wizard(page)).toContainText("Starting sign-in…");
    await page.getByRole("button", { name: "Home", exact: true }).click();
    await expect(wizard(page)).toHaveCount(0);
    release();
    await expect(page.locator(".platform-indicator")).toHaveAccessibleName(
      /Signing in/,
      { timeout: login },
    );
    // The outdated "sign in again" banner gives way to the waiting one.
    await expect(page.locator(".platform-banner")).toContainText(
      "Studio is waiting for you to finish signing in to Acme.",
    );
    expect(host.requests("/studio/connection/login/login-1/cancel")).toEqual(
      [],
    );
    host.finish("authenticated");
    await expect(page.locator(".platform-indicator")).toHaveAccessibleName(
      /Signed in/,
      { timeout: login },
    );
  });

  test("the time limit still applies after the wizard closed", async ({
    page,
  }) => {
    const host = await platformHost(page, {
      platforms: [acmePlatform({ signedIn: false })],
      active: "Acme",
      loginExpiresIn: 2,
    });
    await page
      .locator(".platform-banner")
      .getByRole("button", { name: "Sign in again" })
      .click();
    await page
      .getByRole("button", { name: "Sign in with your browser" })
      .click();
    await expect(
      page.getByRole("textbox", { name: "Sign-in address" }),
    ).toBeVisible({ timeout: login });
    await page.getByRole("button", { name: "Home", exact: true }).click();
    await expect(page.locator(".platform-banner")).toContainText(
      "Sign-in timed out.",
      { timeout: login },
    );
    await expect
      .poll(
        () => host.requests("/studio/connection/login/login-1/cancel").length,
      )
      .toBe(1);
  });
});

test.describe("signing out", () => {
  test("a sign-out the identity provider did not confirm is noted calmly", async ({
    page,
  }) => {
    await platformHost(page, {
      platforms: [acmePlatform(), globexPlatform()],
      active: "Acme",
      revocation: "unconfirmed",
    });
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await page.getByRole("button", { name: "Sign out of Acme" }).click();
    await page
      .getByRole("dialog")
      .getByRole("button", { name: "Sign out" })
      .click();
    const note = page.locator(".platform-banner");
    await expect(note).toHaveText(
      /Signed out on this computer\. Your identity provider did not confirm the sign-out\./,
    );
    await expect(note).toHaveClass(/calm/);
    await expect(note).not.toContainText("WV-");
    await note
      .getByRole("button", { name: "Dismiss platform message" })
      .click();
    await expect(note).toHaveCount(0);

    // Removing a signed-in platform reports the same.
    await page.getByRole("button", { name: "More actions for Globex" }).click();
    await page.getByRole("menuitem", { name: "Remove Globex" }).click();
    await page
      .getByRole("dialog")
      .getByRole("button", { name: "Remove platform" })
      .click();
    await expect(note).toContainText(
      "Signed out on this computer. Your identity provider did not confirm the sign-out.",
    );
  });

  test("a confirmed sign-out shows no note", async ({ page }) => {
    await platformHost(page, { platforms: [acmePlatform()], active: "Acme" });
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await page.getByRole("button", { name: "Sign out of Acme" }).click();
    await page
      .getByRole("dialog")
      .getByRole("button", { name: "Sign out" })
      .click();
    await expect(
      page.getByRole("button", { name: "Sign in to Acme" }),
    ).toBeVisible();
    await expect(page.locator(".platform-banner")).toHaveCount(0);
  });
});

test("an account whose subject the provider didn't share is still described", async ({
  page,
}) => {
  await platformHost(page, {
    platforms: [acmePlatform()],
    active: "Acme",
    test: () => ({
      status: 401,
      json: {
        status: 401,
        code: "WV-AUTH-NOT-LINKED",
        message: "You signed in, but this platform does not know your account.",
        account: {
          provider_id: "acme",
          issuer: acmeOption.issuer,
          subject: null,
          display_name: null,
        },
      },
    }),
  });
  await page
    .locator(".platform-banner")
    .getByRole("button", { name: "See details" })
    .click();
  const details = page.getByLabel("Details for your administrator");
  await expect(details).toHaveValue(
    /Subject: Not shared by your identity provider/,
  );
  await expect(details).toHaveValue(/Identity provider ID: acme/);
  await expect(details).not.toHaveValue(/null/);
});

test("switching to an account the platform doesn't know updates the top bar", async ({
  page,
}) => {
  let linked = true;
  const host = await platformHost(page, {
    platforms: [acmePlatform()],
    active: "Acme",
    test: () =>
      linked
        ? undefined
        : {
            status: 401,
            json: {
              status: 401,
              code: "WV-AUTH-NOT-LINKED",
              message: "You signed in, but this platform does not know you.",
              account: {
                provider_id: "acme",
                issuer: acmeOption.issuer,
                subject: "00u9-new",
                display_name: "new.person@acme.example",
              },
            },
          },
  });
  const indicator = page.locator(".platform-indicator");
  await expect(indicator).toHaveAccessibleName(
    "Platform: Acme · Signed in as jane@acme.example · Workspace Payments / Production",
  );
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByRole("button", { name: "Switch account on Acme" }).click();
  await page.getByRole("button", { name: "Sign in with your browser" }).click();
  await expect
    .poll(() => host.requests("/studio/connection/login/start").length)
    .toBe(1);
  linked = false;
  host.finish("authenticated");
  await expect(
    page.getByRole("heading", {
      name: "You signed in, but this platform doesn't recognize your account yet",
    }),
  ).toBeVisible();
  // The previous account's identity is gone: the top bar says so too.
  await expect(indicator).toHaveAccessibleName(
    /^Platform: Acme · Account not recognized · /,
  );
  // The visible text agrees with the name while the assistant stays open.
  await expect(indicator).toContainText("Account not recognized");
  await expect(indicator).not.toContainText("Connecting…");
  await expect(indicator).not.toContainText("Signed in");
  // Leaving the assistant keeps the explanation one click away.
  await page.getByRole("button", { name: "Home", exact: true }).click();
  const banner = page.locator(".platform-banner");
  await expect(banner).toContainText(
    "You signed in, but Acme doesn't recognize your account yet.",
  );
  await banner.getByRole("button", { name: "See details" }).click();
  await expect(page.getByLabel("Details for your administrator")).toHaveValue(
    /Subject: 00u9-new/,
  );
});

test("a platform call after the sign-in ended shows the calm expired notice", async ({
  page,
}) => {
  const host = await platformHost(page, {
    platforms: [acmePlatform()],
    active: "Acme",
  });
  const indicator = page.locator(".platform-indicator");
  await expect(indicator).toHaveAccessibleName(
    /Signed in as jane@acme\.example/,
  );
  // The host could not renew the access token: the provider ended the session.
  await page.route(
    (url) => url.pathname.endsWith("/runs"),
    (route) =>
      route.fulfill({
        status: 401,
        json: {
          status: 401,
          code: "WV-AUTH-REQUIRED",
          message:
            "Sign in again with weave auth login using the selected profile",
        },
      }),
  );
  await page.getByRole("button", { name: "Runs", exact: true }).click();
  const banner = page.locator(".platform-banner");
  await expect(banner).toContainText(
    "Your session for Acme expired. Sign in again to publish, run, and manage work. You can keep working locally.",
  );
  await expect(page.locator(".error-banner")).toHaveCount(0);
  await expect(page.getByText("weave auth login")).toHaveCount(0);
  await expect(indicator).toHaveAccessibleName(
    "Platform: Acme · Session expired · Workspace Payments / Production",
  );
  await indicator.click();
  await expect(
    page.locator("#platform-menu").getByRole("button", { name: "Sign in" }),
  ).toBeVisible();
  await expect(
    page.locator("#platform-menu").getByRole("button", { name: "Sign out" }),
  ).toHaveCount(0);
  await indicator.click();
  // Local authoring carries on: Validate checks on this computer only.
  await page.getByRole("button", { name: "Home", exact: true }).click();
  await page.getByRole("button", { name: "New workflow", exact: true }).click();
  await page.locator(".palette-step").filter({ hasText: "Transform" }).click();
  await page.getByRole("button", { name: "Validate", exact: true }).click();
  await expect(page.locator(".diagnostics")).toContainText(
    "Sign in again to check actions and connections against the project.",
  );
  expect(
    host.calls.filter((c) => c.path.endsWith("/compiler/compile")),
  ).toHaveLength(0);
  await expect(page.locator(".error-banner")).toHaveCount(0);
  await banner.getByRole("button", { name: "Sign in again" }).click();
  await expect(heading(page)).toHaveText("Sign in to Acme");
});

test("after signing out, lists and Home ask to sign in instead of reporting an expired session", async ({
  page,
}) => {
  await platformHost(page, { platforms: [acmePlatform()], active: "Acme" });
  const indicator = page.locator(".platform-indicator");
  await expect(indicator).toHaveAccessibleName(
    /Signed in as jane@acme\.example/,
  );
  await indicator.click();
  await page
    .locator("#platform-menu")
    .getByRole("button", { name: "Sign out" })
    .click();
  await page
    .getByRole("dialog", { name: "Sign out of Acme?" })
    .getByRole("button", { name: "Sign out" })
    .click();
  await expect(indicator).toHaveAccessibleName(
    /^Platform: Acme · Signed out · /,
  );
  // Like the real host, the platform refuses calls while signed out.
  await page.route(
    (url) => url.pathname.startsWith("/studio/api/"),
    (route) =>
      route.fulfill({
        status: 401,
        json: {
          status: 401,
          code: "WV-AUTH-REQUIRED",
          message: "Sign in with weave auth login and select that profile",
        },
      }),
  );
  await page.getByRole("button", { name: "Home", exact: true }).click();
  const home = page.locator("weave-home-dashboard");
  await expect(home).toContainText("Sign in to see runs.");
  await expect(home).toContainText("Sign in to see your tasks.");
  await expect(home).not.toContainText("No runs yet");
  await page.getByRole("button", { name: "Runs", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Sign in to see runs" }),
  ).toBeVisible();
  await expect(page.getByText("No runs yet")).toHaveCount(0);
  // Nothing expired: the person chose to sign out.
  await expect(indicator).toHaveAccessibleName(
    /^Platform: Acme · Signed out · /,
  );
  await expect(page.locator(".platform-banner")).toHaveCount(0);
  await expect(page.locator(".error-banner")).toHaveCount(0);
  // A calm banner offers the sign-in; the filters wait for it.
  await expect(page.getByLabel("Search by business key")).toBeDisabled();
  await page
    .locator(".records-banner")
    .getByRole("button", { name: "Sign in" })
    .click();
  await expect(heading(page)).toHaveText("Sign in to Acme");
});

test("a list shows only its own rows, labeled with the workspace's names", async ({
  page,
}) => {
  await platformHost(page, { platforms: [acmePlatform()], active: "Acme" });
  await expect(page.locator(".platform-indicator")).toHaveAccessibleName(
    /Signed in as jane@acme\.example/,
  );
  await page.route(
    (url) => url.pathname.endsWith("/runs"),
    (route) =>
      route.fulfill({
        json: {
          items: [{ id: "run-7", state: { status: "succeeded" } }],
          next_cursor: null,
        },
      }),
  );
  let release = () => {};
  const held = new Promise<void>((done) => (release = done));
  await page.route(
    (url) => url.pathname.endsWith("/workflows"),
    async (route) => {
      await held;
      await route.fulfill({ json: { items: [], next_cursor: null } });
    },
  );
  await page.getByRole("button", { name: "Runs", exact: true }).click();
  await expect(page.locator(".resource-row")).toContainText("run-7");
  await expect(page.locator(".scope-label")).toHaveText(
    "Acme · Payments / Production",
  );
  // While Workflows loads, the run rows are gone, not shown under its heading.
  await page.getByRole("button", { name: "Workflows", exact: true }).click();
  await expect(page.getByText("Loading workflows…")).toBeVisible();
  await expect(page.locator(".resource-row")).toHaveCount(0);
  release();
  await expect(
    page.getByRole("heading", { name: "No workflows yet" }),
  ).toBeVisible();
});
