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
// The run-time half of the Studio journeys' no-mocks rule
// (tests/acceptance/harness.ts): Playwright's own fixtures throw, and a
// guarded context and its pages refuse interception, injected state and
// requests other than GET, however the call is spelled.
import { expect } from "@playwright/test";
import { guardContext, guardPage, test } from "../acceptance/harness";

// Listed here, not read from the harness, so a shortened deny list fails.
const DENIED_CONTEXT = [
  "route",
  "unroute",
  "unrouteAll",
  "routeFromHAR",
  "routeWebSocket",
  "setExtraHTTPHeaders",
  "setHTTPCredentials",
  "addCookies",
  "clearCookies",
  "storageState",
  "setOffline",
  "addInitScript",
  "exposeBinding",
  "exposeFunction",
  "newCDPSession",
  "grantPermissions",
  "setGeolocation",
  "browser",
];
const DENIED_PAGE = [
  "route",
  "unroute",
  "unrouteAll",
  "routeFromHAR",
  "routeWebSocket",
  "setExtraHTTPHeaders",
  "addInitScript",
  "addScriptTag",
  "addStyleTag",
  "setContent",
  "exposeBinding",
  "exposeFunction",
];

type Loose = Record<string, (...args: unknown[]) => unknown>;
const handler = () => undefined;

test("Playwright's own browser, context, page and request fixtures throw", async ({
  browser,
  context,
  page,
  request,
}) => {
  for (const fixture of [browser, context, page, request])
    expect(() => (fixture as unknown as Loose)["goto"]).toThrow(
      /get a page only from Journey\.signIn/,
    );
});

test("a guarded context and its pages refuse every denied method", async ({
  journeyBrowser,
  baseURL,
}) => {
  const context = guardContext(await journeyBrowser.newContext());
  // As Journey.signIn: the harness traces the context itself.
  await context.tracing.start({ screenshots: true, snapshots: true });
  const page = guardPage(await context.newPage());
  const later = await context.newPage();
  for (const name of DENIED_CONTEXT)
    expect(() => (context as unknown as Loose)[name]!("**/*", handler)).toThrow(
      `Studio journeys may not use context.${name}`,
    );
  for (const target of [page, later])
    for (const name of DENIED_PAGE)
      expect(() =>
        (target as unknown as Loose)[name]!("**/*", handler),
      ).toThrow(`Studio journeys may not use page.${name}`);

  // The spellings only a run-time guard can see through.
  const loose = page as unknown as Loose;
  const name = ["ro", "ute"].join("");
  const spellings: Record<string, () => unknown> = {
    "computed name": () => loose[name]!("**/*", handler),
    "Reflect.get": () =>
      (Reflect.get(page, "route") as Loose[string]).call(page, "**/*", handler),
    "Reflect.apply": () => Reflect.apply(page.route, page, ["**/*", handler]),
    "call on the method": () => page.route.call(page, "**/*", handler),
    "bound method": () => page.route.bind(page)("**/*", handler),
    "destructured method": () => {
      const { route } = page;
      return route("**/*", handler);
    },
    "page.context()": () => page.context().route("**/*", handler),
    "the context's browser": () => context.browser(),
    "property reassigned": () => {
      loose["route"] = () => "replaced";
    },
  };
  for (const [spelling, call] of Object.entries(spellings))
    expect(call, spelling).toThrow();

  // Navigation stays on http(s): data:, blob:, file:, about: and javascript:
  // pages would replace the page as setContent does.
  for (const url of [
    "data:text/html,<h1>FAKE</h1>",
    "blob:http://127.0.0.1/x",
    "file:///etc/hosts",
    "about:blank",
    "javascript:void(0)",
  ])
    await expect(page.goto(url), url).rejects.toThrow(
      /Studio journeys navigate only to http\(s\) pages/,
    );
  expect((await page.goto(baseURL!))?.ok()).toBe(true);

  // Requests with the person's cookies: GET only.
  expect((await page.request.get(baseURL!)).ok()).toBe(true);
  for (const method of ["post", "put", "patch", "delete", "head", "fetch"])
    for (const owner of [page, context])
      expect(
        () => (owner.request as unknown as Loose)[method]!(baseURL),
        method,
      ).toThrow(/may not use (page|context)\.request\./);

  // The prototypes hold guards too: a destructured-reflection bypass reaches the
  // class's own route through its descriptors, and still throws for this page.
  const { getPrototypeOf: gp, getOwnPropertyDescriptors: gd } = Object;
  for (const target of [page, context]) {
    const [, descriptor] = Object.entries(gd(gp(target))).find(
      ([key]) => key === name,
    ) as [string, PropertyDescriptor];
    expect(() =>
      (descriptor.value as Loose[string]).call(target, "**/*", handler),
    ).toThrow(/Studio journeys may not use (page|context)\.route/);
  }

  // The deny lists cannot be reached to shorten them.
  const harness = (await import("../acceptance/harness")) as Record<
    string,
    unknown
  >;
  expect(harness["DENIED_PAGE"]).toBeUndefined();
  expect(harness["DENIED_CONTEXT"]).toBeUndefined();

  // Closing still works: the trace stops and the context disposes its request through the guard.
  await context.tracing.stop();
  await context.close();
});

test("pages the harness never guarded keep their own methods", async ({
  journeyBrowser,
}) => {
  const guarded = guardContext(await journeyBrowser.newContext());
  guardPage(await guarded.newPage());
  // Mocked suites can share this worker: their contexts still route.
  const context = await journeyBrowser.newContext();
  const page = await context.newPage();
  await page.route("**/never", (route) => route.abort());
  await context.route("**/never", (route) => route.abort());
  await context.close();
  await guarded.close();
});
