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
// The Studio side of the acceptance journeys. A journey is one Playwright
// test; `j.step("J1.1", …)` runs a journeys.toml step and records passed,
// partial, failed, skipped (with the capabilities it waits for) or not_run in
// evidence/steps.jsonl, as the pytest harness does, with the viewport. People
// sign in through the real Studio host and the identity provider's pages, each
// in their own browser context. When the journey ends, every context's
// storage is kept for the canary scan, its requests are audited, and its
// platform is removed through Studio, which signs out and deletes the sign-in.
// Only sign-ins this run can prove are its own are deleted: their bindings
// are computed from the platforms the run added to its own Studio hosts. A
// binding any other process stores meanwhile is reported, never deleted.
import { expect, test as base } from "@playwright/test";
import type {
  APIRequestContext,
  Browser,
  BrowserContext,
  Page,
} from "@playwright/test";
import {
  appendFileSync,
  chmodSync,
  existsSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  writeFileSync,
} from "node:fs";
import { join } from "node:path";
import { credentialAccounts, deleteCredential } from "../credential-store";
import {
  cleanups,
  connectAndSignIn,
  pair,
  removePlatform,
  restoreAccessTokenLifespan,
  session,
  setup,
  uniqueName,
} from "../browser/real-platform-support";
import type { Person } from "../browser/real-platform-support";
import { openSidebar } from "../browser/real-platform-support";
import {
  accountsToDelete,
  deepFreeze,
  dumpName,
  dumpOriginProblem,
  errorLine,
  networkProblems,
  platformBinding,
  shotName,
  stepRefusal,
  redact,
  secretsOf,
  stepOutcome,
  storageKeyCheckOn,
  storageAccountKeys,
  viewportName,
} from "./records";
import type { SkippedCheck, StepRecord, StepState } from "./records";

// Journeys import everything they use from this file (tests/acceptance-lint.ts).
export { expect, openSidebar };

export const runDir = process.env["WEAVE_ACC_RUN_DIR"]?.trim() ?? "";

/**
 * Methods a journey's browser context may not use: request interception and
 * shaping, injected sign-in state, page scripts, browser protocol sessions and
 * the context's browser. Replaced on the instance, so every spelling of the
 * call (a computed name, Reflect, call or bind, destructuring) reaches the
 * replacement.
 */
const DENIED_CONTEXT: readonly string[] = Object.freeze([
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
]);
/** Methods a journey's page may not use, for the same reasons. */
const DENIED_PAGE: readonly string[] = Object.freeze([
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
]);
/** The only methods of `page.request` and `context.request`: the host's readiness probe reads with GET. */
const REQUEST_ALLOWED = new Set(["get", "dispose"]);

function deny(what: string): () => never {
  return () => {
    throw Error(`Studio journeys may not use ${what}`);
  };
}

/** Prototype methods already replaced by a guard that refuses guarded instances. */
const guards = new WeakSet<object>();

/**
 * Replaces each method on the instance, and on its prototype with a wrapper
 * that refuses guarded instances and serves every other one as before (mocked
 * suites may share the worker). Both are non-writable and non-configurable, so
 * neither the instance nor the class's descriptors lead back to the original.
 */
function seal(target: object, names: readonly string[], owner: string): void {
  const prototype = Object.getPrototypeOf(target) as Record<string, unknown>;
  for (const name of names) {
    const original = prototype[name];
    if (typeof original === "function" && !guards.has(original)) {
      const wrapped = function (this: object, ...args: unknown[]): unknown {
        if (guarded.has(this)) deny(`${owner}.${name}`)();
        return (original as (...args: unknown[]) => unknown).apply(this, args);
      };
      guards.add(wrapped);
      Object.defineProperty(prototype, name, {
        value: wrapped,
        writable: false,
        configurable: false,
        enumerable: false,
      });
    }
    if (!guards.has(prototype[name] as object))
      throw Error(`The ${owner} prototype's ${name} is not guarded`);
    Object.defineProperty(target, name, {
      value: deny(`${owner}.${name}`),
      writable: false,
      configurable: false,
      enumerable: false,
    });
  }
}

/** `request` limited to GET: the person's cookies never send anything else. */
function readOnlyRequest(
  request: APIRequestContext,
  owner: string,
): APIRequestContext {
  return new Proxy(request, {
    get(target, key) {
      if (typeof key === "string" && REQUEST_ALLOWED.has(key)) {
        const value = target[key as keyof APIRequestContext] as unknown;
        return typeof value === "function" ? value.bind(target) : value;
      }
      if (key === "then") return undefined;
      throw Error(
        `Studio journeys may not use ${owner}.request.${String(key)}`,
      );
    },
    set() {
      throw Error(`Studio journeys may not change ${owner}.request`);
    },
  });
}

const guarded = new WeakSet<object>();

/** A page whose interception, injection and request methods throw. */
export function guardPage(page: Page): Page {
  if (guarded.has(page)) return page;
  guarded.add(page);
  seal(page, DENIED_PAGE, "page");
  Object.defineProperty(page, "request", {
    value: readOnlyRequest(page.request, "page"),
    writable: false,
    configurable: false,
  });
  return page;
}

/** A context whose interception, injection and request methods throw, and whose pages do too. */
export function guardContext(context: BrowserContext): BrowserContext {
  if (guarded.has(context)) return context;
  guarded.add(context);
  seal(context, DENIED_CONTEXT, "context");
  Object.defineProperty(context, "request", {
    value: readOnlyRequest(context.request, "context"),
    writable: false,
    configurable: false,
  });
  context.on("page", guardPage);
  for (const page of context.pages()) guardPage(page);
  return context;
}

/** Stands in for one of Playwright's own fixtures: any use throws. */
function unavailable<T>(name: string): T {
  return new Proxy(
    {},
    {
      get(_, key) {
        if (key === "then") return undefined;
        throw Error(
          `Studio journeys get a page only from Journey.signIn; Playwright's ${name} fixture is not available`,
        );
      },
    },
  ) as T;
}

/**
 * Playwright's test with its own browser, context, page and request fixtures
 * replaced by stand-ins that throw: a journey's pages come from
 * Journey.signIn, in contexts this harness opens, guards and audits.
 */
export const test = base.extend<
  { context: BrowserContext; page: Page; request: APIRequestContext },
  { journeyBrowser: Browser; browser: Browser }
>({
  journeyBrowser: [
    async ({ playwright, browserName }, use) => {
      // As Playwright's own browser fixture: the launch options come from the configuration.
      const browser = await playwright[browserName].launch();
      await use(browser);
      await browser.close();
    },
    { scope: "worker" },
  ],
  browser: [
    // eslint-disable-next-line no-empty-pattern
    async ({}, use) => use(unavailable<Browser>("browser")),
    { scope: "worker" },
  ],
  // eslint-disable-next-line no-empty-pattern
  context: async ({}, use) => use(unavailable<BrowserContext>("context")),
  // eslint-disable-next-line no-empty-pattern
  page: async ({}, use) => use(unavailable<Page>("page")),
  // eslint-disable-next-line no-empty-pattern
  request: async ({}, use) => use(unavailable<APIRequestContext>("request")),
});
const SERVICE = "firefly-weave";
// scripts/acceptance.py STUDIO_PEOPLE: the scan expects one storage dump per line.
const STUDIO_PEOPLE = "studio-people.jsonl";
// scripts/acceptance_evidence.py AFTER_REMOVAL: the dump taken after the platform was removed.
const AFTER_REMOVAL = ".after-removal";
// scripts/acceptance.py SCAN_CHECK: the scan's storage-key check.
const KEY_CHECK = "J9.4/storage-keys";

interface Enablement {
  profile: string;
  steps: Record<string, StepState>;
  viewports: Record<string, string[]>;
  /** The stage that records each step (scripts/acceptance_journeys.py step_stage). */
  stages: Record<string, string>;
}

let resolved: Enablement | null = null;
/** What this run may record: RUN_DIR/enablement.json, written by the orchestrator. */
export function enablement(): Enablement {
  resolved ??= deepFreeze(
    JSON.parse(
      readFileSync(join(runDir, "enablement.json"), "utf8"),
    ) as Enablement,
  );
  return resolved;
}

/** A person's private file: username, password, subject and grants. Never printed. */
export function person(name: string): Person {
  return JSON.parse(
    readFileSync(join(runDir, "private", "people", `${name}.json`), "utf8"),
  ) as Person;
}

/** Pairing codes the Studio hosts of this journey printed; redacted like any secret. */
const pairingCodes = new Set<string>();

/** The run's canaries, their handles, people passwords and pairing codes, for redaction. */
function secrets(): string[] {
  const canaryFile = join(runDir, "private", "canaries.json");
  const canaries = existsSync(canaryFile)
    ? (JSON.parse(readFileSync(canaryFile, "utf8")) as Record<string, string>)
    : {};
  const folder = join(runDir, "private", "people");
  const passwords = existsSync(folder)
    ? readdirSync(folder)
        .filter((file) => file.endsWith(".json"))
        .map(
          (file) =>
            (
              JSON.parse(readFileSync(join(folder, file), "utf8")) as {
                password?: string;
              }
            ).password,
        )
    : [];
  return [
    ...secretsOf(canaries, passwords, Object.keys(canaries)),
    ...pairingCodes,
  ];
}

function record(entry: StepRecord): void {
  appendFileSync(
    join(runDir, "evidence", "steps.jsonl"),
    JSON.stringify(entry) + "\n",
  );
}

/** A private run file, mode 0600 in a 0700 folder. */
function keepPrivate(folder: string, name: string, content: string): void {
  const directory = join(runDir, "private", folder);
  mkdirSync(directory, { recursive: true, mode: 0o700 });
  const path = join(directory, name);
  writeFileSync(path, content, { mode: 0o600 });
  chmodSync(path, 0o600);
}

/** One line appended to a private run file, mode 0600. */
function appendPrivate(name: string, line: string): void {
  const path = join(runDir, "private", name);
  appendFileSync(path, line + "\n", { mode: 0o600 });
  chmodSync(path, 0o600);
}

interface StorageDump {
  origin: string;
  local: Record<string, string>;
  session: Record<string, string>;
  indexed_db: { database: string; store: string; key: string; value: string }[];
  /** Every IndexedDB database and its stores; only while the storage-key check runs. */
  indexed_db_databases?: { name: string; stores: string[] }[];
}

async function dumpStorage(page: Page): Promise<StorageDump> {
  const listDatabases = storageKeyCheckOn(enablement().steps, KEY_CHECK);
  // read-only audit: lists localStorage, sessionStorage and every IndexedDB database and record without changing them
  return page.evaluate(async (listDatabases) => {
    const take = (storage: Storage) =>
      Object.fromEntries(
        Array.from({ length: storage.length }, (_, index) => storage.key(index))
          .filter((key): key is string => key !== null)
          .map((key) => [key, storage.getItem(key) ?? ""]),
      );
    const indexed: StorageDump["indexed_db"] = [];
    const databases: { name: string; stores: string[] }[] = [];
    for (const info of await indexedDB.databases()) {
      if (!info.name) continue;
      const name = info.name;
      const database = await new Promise<IDBDatabase>((done, fail) => {
        const opening = indexedDB.open(name);
        opening.onsuccess = () => done(opening.result);
        opening.onerror = () => fail(opening.error);
      });
      databases.push({ name, stores: Array.from(database.objectStoreNames) });
      for (const store of Array.from(database.objectStoreNames)) {
        await new Promise<void>((done, fail) => {
          const cursor = database
            .transaction(store, "readonly")
            .objectStore(store)
            .openCursor();
          cursor.onsuccess = () => {
            const row = cursor.result;
            if (!row) return done();
            indexed.push({
              database: name,
              store,
              key: JSON.stringify(row.key),
              value: JSON.stringify(row.value),
            });
            row.continue();
          };
          cursor.onerror = () => fail(cursor.error);
        });
      }
      database.close();
    }
    return {
      origin: location.origin,
      local: take(localStorage),
      session: take(sessionStorage),
      indexed_db: indexed,
      ...(listDatabases ? { indexed_db_databases: databases } : {}),
    };
  }, listDatabases);
}

interface SignedIn {
  name: string;
  /** This sign-in's storage dump: `<journey>-<viewport>-<person>[-<n>]`. */
  dump: string;
  context: BrowserContext;
  page: Page;
  platform: string;
  host: string;
  /** The Studio host's configuration home: its saved platforms name this sign-in's binding. */
  configHome: string;
  urls: string[];
  console: string[];
  signedIn: boolean;
}

export class Journey {
  // Private at run time too, so a journey can neither reset a failure nor
  // edit the running step's record.
  #failed = false;
  #shots = 0;
  #current: {
    id: string;
    ran: Set<string>;
    skipped: SkippedCheck[];
    screenshot?: string;
  } | null = null;
  readonly #people: SignedIn[] = [];

  constructor(
    readonly id: string,
    readonly viewport: string,
    browser: Browser,
    private readonly size: { width: number; height: number },
  ) {
    this.#browser = browser;
  }
  /** Private at run time too: a journey cannot reach the browser to open its own context. */
  readonly #browser: Browser;

  /** Runs one journeys.toml step, or records why it did not run. */
  async step(stepId: string, body: () => Promise<void>): Promise<boolean> {
    const map = enablement();
    const refusal = stepRefusal(stepId, this.id, map.steps, map.stages);
    if (refusal) throw Error(refusal);
    const state = map.steps[stepId]!;
    if (!state.applies) return false;
    if (state.missing.length) {
      record({
        id: stepId,
        status: "skipped",
        missing: state.missing,
        viewport: this.viewport,
      });
      return false;
    }
    if (this.#failed) {
      record({
        id: stepId,
        status: "not_run",
        error: "An earlier step of this journey failed.",
        viewport: this.viewport,
      });
      return false;
    }
    const started = Date.now();
    const current: {
      id: string;
      ran: Set<string>;
      skipped: SkippedCheck[];
      screenshot?: string;
    } = { id: stepId, ran: new Set<string>(), skipped: [] };
    this.#current = current;
    const seconds = () => Math.round((Date.now() - started) / 100) / 10;
    const shot = () =>
      current.screenshot ? { screenshot: current.screenshot } : {};
    try {
      await body();
      const outcome = stepOutcome(
        stepId,
        enablement().steps,
        current.ran,
        current.skipped,
      );
      if (outcome.problems.length) throw Error(outcome.problems.join("; "));
      record({
        id: stepId,
        status: outcome.status,
        seconds: seconds(),
        viewport: this.viewport,
        ...(outcome.skippedChecks.length
          ? { skipped_checks: outcome.skippedChecks }
          : {}),
        ...shot(),
      });
      return true;
    } catch (error) {
      this.#failed = true;
      const line = errorLine(error, secrets());
      record({
        id: stepId,
        status: "failed",
        seconds: seconds(),
        error: line,
        viewport: this.viewport,
        ...shot(),
      });
      // Never the original error: a Playwright message can quote a value the step typed.
      throw new Error(`${stepId} failed: ${line}`);
    } finally {
      this.#current = null;
    }
  }

  /** Runs one check of the running step, or records it as waiting for capabilities. */
  async check(checkId: string, body: () => Promise<void>): Promise<boolean> {
    const step = this.#current;
    if (!step || !checkId.startsWith(`${step.id}/`))
      throw Error(`${checkId} is not a check of the running step`);
    const state = enablement().steps[checkId];
    if (!state)
      throw Error(
        `${checkId} is not declared in tests/acceptance/journeys.toml`,
      );
    if (!state.applies) return false;
    if (state.missing.length) {
      step.skipped.push({ check: checkId, missing: state.missing });
      return false;
    }
    await body();
    step.ran.add(checkId);
    return true;
  }

  /** A new browser context signed in as `name` through the real host and identity provider. */
  async signIn(name: string): Promise<Page> {
    const context = guardContext(
      await this.#browser.newContext({
        viewport: this.size,
        locale: "en-US",
        timezoneId: "UTC",
      }),
    );
    await context.tracing.start({ screenshots: true, snapshots: true });
    const page = guardPage(await context.newPage());
    const prefix = `${this.id.toLowerCase()}-${name}`;
    const ordinal =
      this.#people.filter((earlier) => earlier.name === name).length + 1;
    const signed: SignedIn = {
      name,
      dump: dumpName(this.id, this.viewport, name, ordinal),
      context,
      page,
      platform: uniqueName(prefix),
      host: "",
      configHome: "",
      urls: [],
      console: [],
      signedIn: false,
    };
    // Registered before anything can fail: close() keeps this context's storage
    // and the scan expects a dump for every person a journey signed in as.
    this.#people.push(signed);
    appendPrivate(
      STUDIO_PEOPLE,
      JSON.stringify({
        journey: this.id,
        viewport: this.viewport,
        person: name,
        dump: signed.dump,
      }),
    );
    context.on("request", (request) => signed.urls.push(request.url()));
    page.on("console", (message) =>
      signed.console.push(`${message.type()}: ${message.text()}`),
    );
    const started = await session(page);
    const host = started.host!;
    signed.host = host.origin;
    signed.configHome = started.configHome;
    await expect
      .poll(() => host.output().match(/Pairing code: (\S+)/)?.[1], {
        timeout: 30_000,
      })
      .toBeTruthy();
    pairingCodes.add(host.output().match(/Pairing code: (\S+)/)![1]!);
    await pair(page, host, `${prefix}-pairing`);
    await connectAndSignIn(page, signed.platform, person(name));
    recordBinding(signed);
    signed.signedIn = true;
    return page;
  }

  platformName(name: string): string {
    const found = this.#people.find(
      (signed) => signed.name === name && signed.signedIn,
    );
    if (!found) throw Error(`${name} has not signed in in ${this.id}`);
    return found.platform;
  }

  /** A step screenshot in evidence/screens/<journey>/<viewport>/<nn>-<slug>.png. */
  async shot(page: Page, slug: string): Promise<void> {
    this.#shots += 1;
    const file = shotName(this.#shots, slug);
    const folder = join(runDir, "evidence", "screens", this.id, this.viewport);
    mkdirSync(folder, { recursive: true });
    await page.screenshot({ path: join(folder, file) });
    if (this.#current)
      this.#current.screenshot = ["screens", this.id, this.viewport, file].join(
        "/",
      );
  }

  /** Every person's requests stayed on the Studio host and the identity provider; no mascot artwork. */
  finish(): void {
    // A step that failed fails the test, even when the journey caught its error.
    if (this.#failed) throw Error(`a step of ${this.id} failed`);
    const allowed = [setup().keycloak];
    const problems = this.#people.flatMap(({ name, host, urls }) =>
      networkProblems(urls, [...allowed, host]).map(
        (problem) => `${name}: ${problem}`,
      ),
    );
    expect(
      problems,
      "requests outside the Studio host and the identity provider",
    ).toEqual([]);
  }

  /** Keeps storage and requests for the scan, removes each platform through Studio, closes the contexts. */
  async close(failed: boolean): Promise<void> {
    const problems: string[] = [];
    for (const signed of this.#people) {
      const base = signed.dump;
      try {
        const dump = await dumpStorage(signed.page);
        // Storage read on another origin (the identity provider's page, say) is
        // not this person's Studio storage: no dump, so the scan reports it missing.
        const wrong = dumpOriginProblem(dump.origin, signed.host);
        if (wrong) throw Error(wrong);
        keepPrivate(
          "browser-storage",
          `${base}.json`,
          JSON.stringify({
            journey: this.id,
            viewport: this.viewport,
            person: signed.name,
            ...dump,
          }),
        );
      } catch (error) {
        problems.push(
          `storage of ${signed.name}: ${errorLine(error, secrets())}`,
        );
      }
      keepPrivate(
        "network",
        `${base}.jsonl`,
        signed.urls.map((url) => JSON.stringify({ url })).join("\n"),
      );
      keepPrivate(
        "console",
        `${base}.jsonl`,
        signed.console
          .map((line) => JSON.stringify({ line: redact(line, secrets()) }))
          .join("\n"),
      );
      // A sign-in that failed half-way may still have stored its binding.
      recordBinding(signed);
      let removed = false;
      try {
        if (signed.signedIn) {
          await removePlatform(signed.page, signed.platform);
          removed = true;
        }
      } catch (error) {
        problems.push(
          `removing ${signed.name}'s platform: ${errorLine(error, secrets())}`,
        );
      }
      // Removing the platform purges its account keys: a second dump shows what
      // is left, once the purge has finished or 10 s have passed.
      if (removed)
        try {
          await expect
            .poll(
              async () => storageAccountKeys(await dumpStorage(signed.page)),
              { timeout: 10_000 },
            )
            .toBe(0)
            .catch(() => undefined);
          const dump = await dumpStorage(signed.page);
          const wrong = dumpOriginProblem(dump.origin, signed.host);
          if (wrong) throw Error(wrong);
          keepPrivate(
            "browser-storage",
            `${base}${AFTER_REMOVAL}.json`,
            JSON.stringify({
              journey: this.id,
              viewport: this.viewport,
              person: signed.name,
              ...dump,
            }),
          );
        } catch (error) {
          problems.push(
            `storage of ${signed.name} after removal: ${errorLine(error, secrets())}`,
          );
        }
      await signed.context.tracing.stop(
        failed
          ? {
              path: join(
                runDir,
                "private",
                "playwright",
                "traces",
                `${base}.zip`,
              ),
            }
          : undefined,
      );
      await signed.context.close();
    }
    if (problems.length) throw Error(problems.join("; "));
  }
}

let active: Journey | null = null;
let baseline: Set<string> | undefined;
/** Credential-store bindings of the platforms this run added; the only ones it may delete. */
const ownBindings = new Set<string>();

/** Adds the binding of `signed`'s platform, read from its Studio host's saved platforms. */
function recordBinding(signed: { configHome: string; platform: string }): void {
  if (!signed.configHome) return;
  const path = join(signed.configHome, "profiles.json");
  if (!existsSync(path)) return;
  try {
    const binding = platformBinding(
      JSON.parse(readFileSync(path, "utf8")),
      signed.platform,
    );
    if (binding) ownBindings.add(binding);
  } catch {
    // An unreadable file names no binding; nothing of it is deleted.
  }
}

/** Registers the suite hooks: credential-store baseline and cleanup. Call once per spec file. */
export function useJourneys(): void {
  test.skip(
    !runDir,
    "Studio journeys run through scripts/acceptance.py (WEAVE_ACC_RUN_DIR).",
  );
  test.beforeAll(() => {
    baseline = undefined;
    baseline = credentialAccounts(SERVICE);
  });
  test.afterEach(async ({}, info) => {
    const failed = info.status !== info.expectedStatus;
    const journey = active;
    active = null;
    let closing: unknown = null;
    try {
      await journey?.close(failed);
    } catch (error) {
      closing = error;
    }
    await restoreAccessTokenLifespan();
    // A failed test's cleanup prints the Studio host's output, which holds the
    // pairing code: everything printed meanwhile goes through redaction.
    const print = console.error;
    console.error = (...parts: unknown[]) =>
      print(redact(parts.map(String).join(" "), secrets()));
    try {
      while (cleanups.length)
        try {
          await cleanups.pop()!(info);
        } catch (error) {
          console.error(errorLine(error, secrets()));
        }
    } finally {
      console.error = print;
    }
    if (failed || closing)
      for (const account of accountsToDelete(
        ownBindings,
        credentialAccounts(SERVICE),
      ))
        deleteCredential(SERVICE, account);
    if (closing) throw closing;
  });
  test.afterAll(() => {
    const now = credentialAccounts(SERVICE);
    const left = accountsToDelete(ownBindings, now);
    for (const account of left) deleteCredential(SERVICE, account);
    // Another process (the person's own Studio, another suite) may sign in
    // meanwhile: its bindings are reported and kept.
    const unknown = baseline
      ? [...now].filter((a) => !baseline!.has(a) && !ownBindings.has(a))
      : [];
    if (unknown.length)
      console.warn(
        `${unknown.length} credential(s) another process stored during the run were left in place`,
      );
    expect(left, "credentials this run left in the system store").toEqual([]);
  });
}

/** One journey at the project's viewport, when this profile records it there. */
export function journey(
  id: string,
  title: string,
  body: (j: Journey) => Promise<void>,
): void {
  test(`${id}: ${title}`, async ({ journeyBrowser }, info) => {
    const size = info.project.use.viewport ?? null;
    const viewport = viewportName(size);
    test.skip(
      !(enablement().viewports[id] ?? []).includes(viewport),
      `${id} is not recorded at ${viewport} in the ${enablement().profile} profile`,
    );
    const j = new Journey(id, viewport, journeyBrowser, size!);
    active = j;
    await body(j);
    j.finish();
  });
}
