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
import { describe, expect, it } from "vitest";
import {
  defaultRunsTabFilters,
  emptyText,
  filterCount,
  MAX_LISTED_RUNS,
  refreshed,
  runRow,
  runsTabMode,
  runsTabQuery,
  type RunsTabFilters,
} from "../src/app/editor/runs-tab/runs-list-model";
import {
  runViewQuery,
  type RunSummary,
  type RunSummaryItem,
} from "../src/app/operate/run-contracts";
import {
  fixtureIds,
  runViewsFixture,
} from "../src/app/operate/run-views.fixture";
import { classifyQueriesWithPython, pythonAvailable } from "./run-views-oracle";

const summary = (id: string): RunSummary =>
  runViewsFixture.runs.find((run) => run.summary.id === id)!.summary;
const now = new Date("2026-10-07T12:00:00Z");
const narrowed: RunsTabFilters = {
  status: ["failed", "waiting"],
  version: "1.2.0",
  origin: "webhook",
  includeTest: false,
};

describe("the list's query", () => {
  it("asks for the workflow's runs newest first with test runs", () => {
    expect(
      runsTabQuery("invoice-approval", defaultRunsTabFilters()),
    ).toStrictEqual({
      workflow: "invoice-approval",
      include_test: true,
      order: "started_desc",
      limit: 50,
    });
    expect(runsTabQuery("invoice-approval", narrowed, "c2")).toStrictEqual({
      workflow: "invoice-approval",
      include_test: false,
      order: "started_desc",
      limit: 50,
      status: ["failed", "waiting"],
      version: "1.2.0",
      origin: "webhook",
      cursor: "c2",
    });
    // The platform reads a version only within one workflow.
    expect(runsTabQuery("", narrowed)).not.toHaveProperty("version");
  });

  describe.skipIf(!pythonAvailable)(
    "with the repository's Python (skipped without it)",
    () => {
      it("every query it sends is one the platform accepts", () => {
        const queries = [
          runsTabQuery("invoice-approval", defaultRunsTabFilters()),
          runsTabQuery("invoice-approval", narrowed, "c2"),
          runsTabQuery("invoice-approval", {
            ...defaultRunsTabFilters(),
            status: ["queued", "running", "suspended", "timed_out"],
          }),
        ].map((query) => runViewQuery(query).slice(1));
        expect(classifyQueriesWithPython(queries)).toEqual(["ok", "ok", "ok"]);
      });
    },
  );
});

describe("what the Runs tab can list", () => {
  const profile = { environmentId: "4a1b2c3d-5e6f-4a7b-8c9d-0e1f2a3b4c5d" };
  const identity = { principal_id: "person" };
  const signedOut = false;
  it("lists only a named workflow's runs", () => {
    expect(
      runsTabMode({ profile, identity, signedOut, name: "invoice-approval" }),
    ).toBe("ready");
    // Without a name the query would name no workflow, so every run in the
    // environment would list: the tab asks for a name instead.
    expect(runsTabMode({ profile, identity, signedOut, name: "" })).toBe(
      "unnamed",
    );
    expect(runsTabMode({ profile, identity, signedOut, name: "  " })).toBe(
      "unnamed",
    );
  });
  it("says what it waits for before a name matters", () => {
    expect(runsTabMode({ profile: null, identity, signedOut, name: "" })).toBe(
      "offline",
    );
    expect(runsTabMode({ profile: {}, identity, signedOut, name: "a" })).toBe(
      "no-environment",
    );
    // Still finding out who is signed in.
    expect(runsTabMode({ profile, identity: null, signedOut, name: "a" })).toBe(
      "pending",
    );
  });
  it("asks to sign in when nobody is signed in, instead of waiting", () => {
    // Signing out keeps the platform and forgets the identity: nothing
    // would end the wait.
    expect(
      runsTabMode({ profile, identity: null, signedOut: true, name: "a" }),
    ).toBe("signed-out");
    // A sign-in that ended may still hold the identity it read.
    expect(runsTabMode({ profile, identity, signedOut: true, name: "a" })).toBe(
      "signed-out",
    );
  });
  it("says the account isn't linked, where signing in again won't help", () => {
    // The platform signed the person in but doesn't know the account: the
    // sign-in counts as ended, yet only an administrator can link it.
    expect(
      runsTabMode({
        profile,
        identity: null,
        signedOut: true,
        notLinked: true,
        name: "a",
      }),
    ).toBe("not-linked");
  });
  it("says why when the check of who is signed in ended without an answer", () => {
    // Nothing would end the wait: no check is out any more.
    expect(
      runsTabMode({
        profile,
        identity: null,
        signedOut,
        unread: "unreachable",
        name: "a",
      }),
    ).toBe("unreachable");
    expect(
      runsTabMode({
        profile,
        identity: null,
        signedOut,
        unread: "store",
        name: "a",
      }),
    ).toBe("store");
  });
});

describe("a run as a list row", () => {
  it("reads a run as one row", () => {
    const failed = runRow(summary(fixtureIds.failed), now);
    expect(failed).toMatchObject({
      id: fixtureIds.failed,
      short: "1c0d7f8a",
      label: "Failed",
      tone: "danger",
      failedAt: "Failed at send",
      at: "send",
      version: "1.3.0",
      duration: "1 min 5 s",
      started: "2 hours ago",
      test: false,
      origin: { label: "Schedule", icon: "schedule" },
      link: null,
      unavailable: false,
    });
    expect(failed.startedTitle).toContain("7 Oct");

    const retry = runRow(summary(fixtureIds.retry), now);
    expect(retry.link).toBe("Retry of 1c0d7f8a");
    expect(retry.label).toBe("Succeeded with errors");
    expect(retry.origin).toEqual({ label: "Retry", icon: "refresh" });
    expect(retry.failedAt).toBeNull();
    expect(retry.at).toBeNull();

    const child = runRow(summary(fixtureIds.child), now);
    expect(child.link).toBe("Called by run 1c0d7f8a");
    expect(child.origin).toEqual({
      label: "Called by a workflow",
      icon: "calledTrigger",
    });

    // A run still going counts its duration up from its start.
    const running = summary(fixtureIds.running);
    expect(runRow(running, now).duration).toBe("30 min");
    expect(runRow(running, new Date("2026-10-07T12:00:30Z")).duration).toBe(
      "30 min 30 s",
    );

    const test = runRow(summary(fixtureIds.test), now);
    expect(test).toMatchObject({
      test: true,
      label: "Waiting",
      version: "1.3.0+test.d3c29f400322",
      origin: { label: "Test", icon: "runHistory" },
    });

    const succeeded = runRow(summary(fixtureIds.succeeded), now);
    expect(succeeded.origin).toEqual({ label: "Webhook", icon: "webhook" });
    // A run a platform event started.
    expect(
      runRow({ ...summary(fixtureIds.succeeded), origin: "event" }, now).origin,
    ).toEqual({ label: "Platform event", icon: "signal" });
    // An older run that doesn't say how it started.
    expect(
      runRow({ ...summary(fixtureIds.succeeded), origin: null }, now).origin,
    ).toBeNull();
  });

  it("a run the platform can't show reads only its ID", () => {
    const row = runRow(
      {
        id: fixtureIds.unavailable,
        unavailable: true,
        omissions: [{ path: "", reason: "classification_unavailable" }],
      },
      now,
    );
    expect(row).toStrictEqual({
      id: fixtureIds.unavailable,
      short: "8d7e4afb",
      label: "Unavailable",
      tone: "neutral",
      started: "",
      startedTitle: "",
      duration: "",
      version: "",
      test: false,
      origin: null,
      link: null,
      failedAt: null,
      at: null,
      unavailable: true,
    });
  });
});

describe("a refresh of the first page", () => {
  it("puts new runs first and keeps the older ones loaded", () => {
    const [a, b, c, d] = [
      fixtureIds.running,
      fixtureIds.retry,
      fixtureIds.failed,
      fixtureIds.succeeded,
    ].map(summary);
    const changed: RunSummary = { ...b, status: "failed" };
    const ids = (items: RunSummaryItem[]) => items.map((item) => item.id);
    const after = refreshed([b, c, d], [a, changed]);
    expect(ids(after)).toEqual([a.id, b.id, c.id, d.id]);
    // The first page's copy of a run replaces the one loaded before.
    expect(after[1]).toBe(changed);
  });

  it("keeps at most 500 runs", () => {
    const many = Array.from({ length: 600 }, (_, index) => ({
      ...summary(fixtureIds.succeeded),
      id: `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`,
    }));
    const after = refreshed(many.slice(50), many.slice(0, 50));
    expect(after).toHaveLength(MAX_LISTED_RUNS);
    expect(after[0].id).toBe(many[0].id);
    expect(after[499].id).toBe(many[499].id);
  });
});

describe("an empty list", () => {
  it("says why the list is empty", () => {
    expect(filterCount(defaultRunsTabFilters())).toBe(0);
    expect(filterCount(narrowed)).toBe(4);
    expect(emptyText("invoice-approval", defaultRunsTabFilters())).toEqual({
      title: "No runs of invoice-approval yet.",
      detail: "Runs appear here once it's activated and started.",
      clear: false,
    });
    expect(
      emptyText("invoice-approval", {
        ...defaultRunsTabFilters(),
        includeTest: false,
      }),
    ).toEqual({
      title: "No runs match these filters.",
      detail: "",
      clear: true,
    });
  });
});
