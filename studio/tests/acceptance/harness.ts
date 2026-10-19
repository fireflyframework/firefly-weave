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
// Sign-ins this run stored are deleted only when the store was read before
// the run started: without that baseline nothing is deleted.
import { Browser, BrowserContext, expect, Page, test } from "@playwright/test";
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
import {
  accountsToDelete,
  dumpName,
  dumpOriginProblem,
  errorLine,
  networkProblems,
  redact,
  secretsOf,
  stepOutcome,
  viewportName,
} from "./records";
import type { SkippedCheck, StepRecord, StepState } from "./records";

export const runDir = process.env["WEAVE_ACC_RUN_DIR"]?.trim() ?? "";
const SERVICE = "firefly-weave";
// scripts/acceptance.py STUDIO_PEOPLE: the scan expects one storage dump per line.
const STUDIO_PEOPLE = "studio-people.jsonl";
// scripts/acceptance_evidence.py AFTER_REMOVAL: the dump taken after the platform was removed.
const AFTER_REMOVAL = ".after-removal";

interface Enablement {
  profile: string;
  steps: Record<string, StepState>;
  viewports: Record<string, string[]>;
}

let resolved: Enablement | null = null;
/** What this run may record: RUN_DIR/enablement.json, written by the orchestrator. */
export function enablement(): Enablement {
  resolved ??= JSON.parse(
    readFileSync(join(runDir, "enablement.json"), "utf8"),
  ) as Enablement;
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
}

async function dumpStorage(page: Page): Promise<StorageDump> {
  // read-only audit: lists localStorage, sessionStorage and every IndexedDB record without changing them
  return page.evaluate(async () => {
    const take = (storage: Storage) =>
      Object.fromEntries(
        Array.from({ length: storage.length }, (_, index) => storage.key(index))
          .filter((key): key is string => key !== null)
          .map((key) => [key, storage.getItem(key) ?? ""]),
      );
    const indexed: StorageDump["indexed_db"] = [];
    for (const info of await indexedDB.databases()) {
      if (!info.name) continue;
      const name = info.name;
      const database = await new Promise<IDBDatabase>((done, fail) => {
        const opening = indexedDB.open(name);
        opening.onsuccess = () => done(opening.result);
        opening.onerror = () => fail(opening.error);
      });
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
    };
  });
}

interface SignedIn {
  name: string;
  /** This sign-in's storage dump: `<journey>-<viewport>-<person>[-<n>]`. */
  dump: string;
  context: BrowserContext;
  page: Page;
  platform: string;
  host: string;
  urls: string[];
  console: string[];
  signedIn: boolean;
}

export class Journey {
  private failed = false;
  private shots = 0;
  private current: {
    id: string;
    ran: Set<string>;
    skipped: SkippedCheck[];
    screenshot?: string;
  } | null = null;
  private readonly people: SignedIn[] = [];

  constructor(
    readonly id: string,
    readonly viewport: string,
    private readonly browser: Browser,
    private readonly size: { width: number; height: number },
  ) {}

  /** Runs one journeys.toml step, or records why it did not run. */
  async step(stepId: string, body: () => Promise<void>): Promise<boolean> {
    const state = enablement().steps[stepId];
    if (!state)
      throw Error(
        `${stepId} is not declared in tests/acceptance/journeys.toml`,
      );
    if (!stepId.startsWith(`${this.id}.`) || stepId.includes("/"))
      throw Error(`${stepId} is not a step of ${this.id}`);
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
    if (this.failed) {
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
    this.current = current;
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
      this.failed = true;
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
      this.current = null;
    }
  }

  /** Runs one check of the running step, or records it as waiting for capabilities. */
  async check(checkId: string, body: () => Promise<void>): Promise<boolean> {
    const step = this.current;
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
    const context = await this.browser.newContext({
      viewport: this.size,
      locale: "en-US",
      timezoneId: "UTC",
    });
    await context.tracing.start({ screenshots: true, snapshots: true });
    const page = await context.newPage();
    const prefix = `${this.id.toLowerCase()}-${name}`;
    const ordinal =
      this.people.filter((earlier) => earlier.name === name).length + 1;
    const signed: SignedIn = {
      name,
      dump: dumpName(this.id, this.viewport, name, ordinal),
      context,
      page,
      platform: uniqueName(prefix),
      host: "",
      urls: [],
      console: [],
      signedIn: false,
    };
    // Registered before anything can fail: close() keeps this context's storage
    // and the scan expects a dump for every person a journey signed in as.
    this.people.push(signed);
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
    await expect
      .poll(() => host.output().match(/Pairing code: (\S+)/)?.[1], {
        timeout: 30_000,
      })
      .toBeTruthy();
    pairingCodes.add(host.output().match(/Pairing code: (\S+)/)![1]!);
    await pair(page, host, `${prefix}-pairing`);
    await connectAndSignIn(page, signed.platform, person(name));
    signed.signedIn = true;
    return page;
  }

  platformName(name: string): string {
    const found = this.people.find(
      (signed) => signed.name === name && signed.signedIn,
    );
    if (!found) throw Error(`${name} has not signed in in ${this.id}`);
    return found.platform;
  }

  /** A step screenshot in evidence/screens/<journey>/<viewport>/<nn>-<slug>.png. */
  async shot(page: Page, slug: string): Promise<void> {
    this.shots += 1;
    const file = `${String(this.shots).padStart(2, "0")}-${slug}.png`;
    const folder = join(runDir, "evidence", "screens", this.id, this.viewport);
    mkdirSync(folder, { recursive: true });
    await page.screenshot({ path: join(folder, file) });
    if (this.current)
      this.current.screenshot = ["screens", this.id, this.viewport, file].join(
        "/",
      );
  }

  /** Every person's requests stayed on the Studio host and the identity provider; no mascot artwork. */
  finish(): void {
    const allowed = [setup().keycloak];
    const problems = this.people.flatMap(({ name, host, urls }) =>
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
    for (const signed of this.people) {
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
      // Removing the platform purges its account keys: a second dump shows what is left.
      if (removed)
        try {
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
let before: Set<string> | undefined;

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
  test.beforeEach(() => {
    before = undefined;
    before = credentialAccounts(SERVICE);
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
        before,
        credentialAccounts(SERVICE),
      ))
        deleteCredential(SERVICE, account);
    if (closing) throw closing;
  });
  test.afterAll(() => {
    const left = accountsToDelete(baseline, credentialAccounts(SERVICE));
    for (const account of left) deleteCredential(SERVICE, account);
    expect(left, "credentials this run left in the system store").toEqual([]);
  });
}

/** One journey at the project's viewport, when this profile records it there. */
export function journey(
  id: string,
  title: string,
  body: (j: Journey) => Promise<void>,
): void {
  test(`${id}: ${title}`, async ({ browser }, info) => {
    const size = info.project.use.viewport ?? null;
    const viewport = viewportName(size);
    test.skip(
      !(enablement().viewports[id] ?? []).includes(viewport),
      `${id} is not recorded at ${viewport} in the ${enablement().profile} profile`,
    );
    const j = new Journey(id, viewport, browser, size!);
    active = j;
    await body(j);
    j.finish();
  });
}
