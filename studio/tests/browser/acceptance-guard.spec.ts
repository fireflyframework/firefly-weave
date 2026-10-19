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
import {
  DENIED_CONTEXT,
  DENIED_PAGE,
  guardContext,
  guardPage,
  test,
} from "../acceptance/harness";

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

  // Requests with the person's cookies: GET only.
  expect((await page.request.get(baseURL!)).ok()).toBe(true);
  for (const method of ["post", "put", "patch", "delete", "head", "fetch"])
    for (const owner of [page, context])
      expect(
        () => (owner.request as unknown as Loose)[method]!(baseURL),
        method,
      ).toThrow(/may not use (page|context)\.request\./);

  // The class keeps its methods: a prototype descriptor still reaches the
  // original, which only the static rule refuses in a journey.
  const original = Object.getOwnPropertyDescriptor(
    Object.getPrototypeOf(page),
    "route",
  )?.value as unknown;
  expect(typeof original).toBe("function");
  expect(original).not.toBe(page.route);

  // Closing still works: the trace stops and the context disposes its request through the guard.
  await context.tracing.stop();
  await context.close();
});
