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
// The Starts tab of a platform event trigger: what it started in the
// environment Studio is connected to, the stop Studio still owes with the
// same checks as the activation dialog, and nothing from an environment, an
// account or a workflow the person left while it was read.
import { describe, expect, it } from "vitest";
import type { PendingStop } from "../src/app/editor/activation/event-bindings";
import {
  EventStartsList,
  owedCallout,
  type StartsTarget,
} from "../src/app/editor/activation/event-starts-list";
import {
  ENV,
  FakePlatform,
  refusal,
  requested,
  startedRuns,
  type Seed,
} from "./event-platform";

const PROJECT = "/studio/api/api/v1/tenants/t/projects/p";
const CURRENT: Seed = {
  id: "t-2",
  name: "notify-team-2",
  activation: "a-new",
  revision: 1,
};
const OLD: Seed = {
  id: "t-1",
  name: "notify-team",
  activation: "a-old",
  revision: 3,
};
const OWED: PendingStop = {
  triggerId: "t-1",
  revision: 3,
  activationId: "a-old",
};
const VERSIONS: Record<string, string> = {
  "a-new": "1.2.0",
  "a-old": "1.1.0",
  "a-older": "1.0.0",
};

/** The platform, with the activations and versions Starts names a trigger's version by. */
class Platform extends FakePlatform {
  project = PROJECT;
  /** The workflow an activation's record names, when it isn't notify-team. */
  readonly runs = new Map<string, string>();
  /** The next answer to a request with this key waits here, after the platform read it. */
  readonly held = new Map<string, Promise<void>>();
  private async hold(key: string) {
    const held = this.held.get(key);
    this.held.delete(key);
    await held;
  }
  override async request<T>(path: string, method = "GET"): Promise<T> {
    const answer = await this.answer<T>(path, method);
    await this.hold(`${method} ${path.split("?")[0]!.replace(ENV, "")}`);
    return answer;
  }
  override async mutate<T>(
    path: string,
    method: string,
    body: unknown,
    revision?: number,
  ): Promise<T> {
    const answer = await super.mutate<T>(path, method, body, revision);
    await this.hold(`${method} ${path.replace(ENV, "")}`);
    return answer;
  }
  private async answer<T>(path: string, method: string): Promise<T> {
    const activation = /\/activations\/(.+)$/.exec(path)?.[1];
    if (activation) {
      this.sent.push({ key: `GET /activations/${activation}` });
      const refused = this.refuse.get(`GET /activations/${activation}`);
      if (refused) throw refused;
      return {
        name: this.runs.get(activation) ?? "notify-team",
        request: { version_id: `v-${activation}` },
      } as T;
    }
    const version = /\/workflows\/v-(.+)$/.exec(path)?.[1];
    if (version) return { version: VERSIONS[version] } as T;
    return super.request<T>(path, method);
  }
}

function setup(
  seeds: Seed[] = [CURRENT],
  overrides: Partial<StartsTarget> = {},
) {
  const platform = new Platform();
  for (const seed of seeds) platform.seed(seed);
  const target: { now: StartsTarget | null } = {
    now: {
      api: platform,
      scope: "person-1",
      environmentId: "env-1",
      environment: "development",
      workflow: "notify-team",
      triggerId: "t-2",
      pendingStops: [],
      ...overrides,
    },
  };
  const settled: [string, string][] = [];
  let clock = Date.parse("2026-11-20T10:00:00Z");
  const list = new EventStartsList({
    target: () => target.now,
    settle: (environmentId, triggerId) =>
      settled.push([environmentId, triggerId]),
    now: () => clock,
  });
  return {
    platform,
    target,
    settled,
    list,
    wait: (ms: number) => (clock += ms),
    show: async () => {
      list.show();
      await list.settled();
    },
  };
}
const kind = (f: ReturnType<typeof setup>) => f.list.state().kind;
const listed = (f: ReturnType<typeof setup>) => {
  const state = f.list.state();
  if (state.kind !== "list") throw Error(`No list: ${state.kind}`);
  return state;
};
/** An answer held back: `release` lets it go. */
function held(f: ReturnType<typeof setup>, key: string) {
  let release = () => undefined as void;
  f.platform.held.set(
    key,
    new Promise<void>((resolve) => (release = () => resolve())),
  );
  return () => release();
}
/** Everything the list does now, up to what the platform holds. */
const settledSoFar = async () => {
  for (let turn = 0; turn < 50; turn++) await Promise.resolve();
};
const callout = (f: ReturnType<typeof setup>) => {
  const owed = f.list.owed();
  return owed && owedCallout(owed);
};

describe("what the trigger started", () => {
  it("is listed newest first with the trigger, its version and the only workflow it reacts to", async () => {
    const f = setup();
    f.platform.starts.set("t-2", [requested("s-1"), ...startedRuns(2)]);
    expect(kind(f)).toBe("loading");
    await f.show();
    const state = listed(f);
    expect(state.trigger).toEqual({
      name: "notify-team-2",
      version: "1.2.0",
      stopped: false,
    });
    expect(state.rows.map((row) => row.title)).toEqual([
      "Waiting to start",
      "Started run r-0",
      "Started run r-1",
    ]);
    expect(state.rows[0]!.detail).toBe(
      "Needs attention in run r-1 of move-between-systems",
    );
    expect(state.readAt).toBe(Date.parse("2026-11-20T10:00:00Z"));
    expect(f.list.owed()).toBeNull();
  });

  it("names no source workflow when the trigger reacts to several", async () => {
    for (const sources of [undefined, ["a", "b"]]) {
      const f = setup([{ ...CURRENT, sources }]);
      f.platform.starts.set("t-2", [requested("s-1")]);
      await f.show();
      expect(listed(f).rows[0]!.detail).toBe("Needs attention in run r-1");
    }
  });

  it("is read once, however often it is drawn", async () => {
    const f = setup();
    await f.show();
    const before = f.platform.sent.length;
    f.list.show();
    f.list.show();
    await f.list.settled();
    expect(f.platform.sent).toHaveLength(before);
  });

  it("shows twenty at a time, and the next page after them", async () => {
    const f = setup();
    f.platform.starts.set("t-2", startedRuns(45));
    await f.show();
    expect(listed(f).rows).toHaveLength(20);
    expect(listed(f).next).not.toBeNull();
    expect(await f.list.more()).toBe("s-20");
    expect(listed(f).rows).toHaveLength(40);
    expect(await f.list.more()).toBe("s-40");
    expect(listed(f).rows).toHaveLength(45);
    expect(listed(f).next).toBeNull();
  });

  it("keeps what it read when the next page fails, and says so", async () => {
    const f = setup();
    f.platform.starts.set("t-2", startedRuns(25));
    await f.show();
    f.platform.refuse.set(
      "GET /event-triggers/t-2/starts",
      refusal(503, "WV-UNAVAILABLE", "Try again later."),
    );
    expect(await f.list.more()).toBeNull();
    expect(listed(f)).toMatchObject({ more: "failed" });
    expect(listed(f).rows).toHaveLength(20);
    f.wait(5_000);
    expect(await f.list.more()).toBe("s-20");
    expect(listed(f)).toMatchObject({ more: "idle" });
  });

  it("keeps the list on the screen while it refreshes", async () => {
    const f = setup();
    f.platform.starts.set("t-2", [requested("s-1")]);
    await f.show();
    f.platform.starts.set("t-2", startedRuns(1));
    const refreshed = f.list.refresh();
    expect(listed(f)).toMatchObject({ refreshing: true });
    await refreshed;
    expect(listed(f)).toMatchObject({ refreshing: false });
    expect(listed(f).rows[0]!.title).toBe("Started run r-0");
  });

  it("asks for the next page once, however often Show more is pressed", async () => {
    const f = setup();
    f.platform.starts.set("t-2", startedRuns(45));
    await f.show();
    const before = f.platform.sent.length;
    expect(await Promise.all([f.list.more(), f.list.more()])).toEqual([
      "s-20",
      null,
    ]);
    expect(f.platform.sent).toHaveLength(before + 1);
    expect(listed(f).rows).toHaveLength(40);
  });

  it("shows no more rows while it refreshes: the next page follows the rows it reads", async () => {
    const f = setup();
    f.platform.starts.set("t-2", startedRuns(45));
    await f.show();
    f.platform.starts.set("t-2", [
      requested("n-1"),
      requested("n-2"),
      ...startedRuns(45),
    ]);
    const refreshed = f.list.refresh();
    expect(await f.list.more()).toBeNull();
    await refreshed;
    expect(await f.list.more()).toBe("s-18");
    const ids = listed(f).rows.map((row) => row.id);
    expect(ids).toEqual([
      "n-1",
      "n-2",
      ...Array.from({ length: 38 }, (_, index) => `s-${index}`),
    ]);
  });

  it("drops a page that answers after a refresh", async () => {
    const f = setup();
    f.platform.starts.set("t-2", startedRuns(45));
    await f.show();
    const release = held(f, "GET /event-triggers/t-2/starts");
    const more = f.list.more();
    await settledSoFar();
    await f.list.refresh();
    release();
    expect(await more).toBeNull();
    expect(listed(f).rows).toHaveLength(20);
    expect(listed(f)).toMatchObject({ more: "idle", refreshing: false });
  });

  it("drops a page that answers once the person left", async () => {
    const f = setup([CURRENT, { ...OLD, id: "t-9", name: "notify-team-9" }]);
    f.platform.starts.set("t-2", startedRuns(45));
    f.platform.starts.set("t-9", [requested("n-1")]);
    await f.show();
    const release = held(f, "GET /event-triggers/t-2/starts");
    const more = f.list.more();
    await settledSoFar();
    f.target.now = { ...f.target.now!, triggerId: "t-9" };
    release();
    // Not redrawn yet: what is shown is still the trigger they left.
    expect(await more).toBeNull();
    expect(listed(f).rows).toHaveLength(20);
    await f.show();
    expect(listed(f).rows.map((row) => row.id)).toEqual(["n-1"]);
  });

  it("keeps the newer of two reads, whichever answers last", async () => {
    const f = setup();
    f.platform.starts.set("t-2", [requested("s-1")]);
    await f.show();
    const release = held(f, "GET /event-triggers/t-2/starts");
    const older = f.list.refresh();
    await settledSoFar();
    f.platform.starts.set("t-2", [requested("s-2"), requested("s-1")]);
    await f.list.refresh();
    release();
    await older;
    expect(listed(f).rows.map((row) => row.id)).toEqual(["s-2", "s-1"]);
  });

  it("says a trigger that was stopped is", async () => {
    const f = setup([{ ...CURRENT, disabled: true }]);
    await f.show();
    expect(listed(f).trigger.stopped).toBe(true);
  });

  it("leaves the version out when it can't be read", async () => {
    const f = setup();
    f.platform.refuse.set(
      "GET /activations/a-new",
      refusal(403, "WV-FORBIDDEN", "Access denied"),
    );
    await f.show();
    expect(listed(f).trigger.version).toBeNull();
  });
});

describe("when there is nothing to list", () => {
  it("says so while Studio isn't connected", async () => {
    const f = setup();
    f.target.now = null;
    await f.show();
    expect(kind(f)).toBe("offline");
    expect(f.platform.sent).toEqual([]);
  });

  it("says so without a trigger here, when it is gone, or when it is another workflow's", async () => {
    const none = setup([], { triggerId: undefined });
    await none.show();
    expect(kind(none)).toBe("none");
    expect(none.platform.sent).toEqual([]);
    const gone = setup([]);
    await gone.show();
    expect(kind(gone)).toBe("none");
    const other = setup([{ ...CURRENT, activation: "a-other" }]);
    await other.show();
    expect(kind(other)).toBe("none");
    expect(other.platform.sent).toHaveLength(1);
  });

  it.each(["GET /event-triggers/t-2", "GET /event-triggers/t-2/starts"])(
    "says so without the permission (%s)",
    async (key) => {
      const f = setup();
      f.platform.refuse.set(key, refusal(403, "WV-FORBIDDEN", "Access denied"));
      await f.show();
      expect(kind(f)).toBe("forbidden");
    },
  );

  it.each(["GET /event-triggers/t-2", "GET /event-triggers/t-2/starts"])(
    "tries a failed read again only after the pause, and only when asked (%s)",
    async (key) => {
      const f = setup();
      const fail = () =>
        f.platform.refuse.set(
          key,
          refusal(503, "WV-UNAVAILABLE", "Try again later."),
        );
      fail();
      await f.show();
      const asked = f.platform.sent.length;
      expect(f.list.state()).toMatchObject({ kind: "failed" });
      const { retryAt } = f.list.state() as { retryAt: number };
      expect(retryAt - Date.parse("2026-11-20T10:00:00Z")).toBe(5_000);
      f.list.show();
      await f.list.refresh();
      expect(f.platform.sent).toHaveLength(asked);
      f.wait(5_000);
      fail();
      await f.list.refresh();
      const again = f.list.state() as { retryAt: number };
      expect(again.retryAt - Date.parse("2026-11-20T10:00:05Z")).toBe(10_000);
      f.wait(10_000);
      await f.list.refresh();
      expect(kind(f)).toBe("list");
    },
  );
});

describe("an answer for what the person left", () => {
  it.each<[string, (target: StartsTarget, platform: Platform) => void]>([
    [
      "another environment",
      (_target, platform) =>
        (platform.environment = `${PROJECT}/environments/other`),
    ],
    ["another account", (target) => (target.scope = "person-2")],
    ["another workflow", (target) => (target.workflow = "sync-references")],
    ["another trigger", (target) => (target.triggerId = "t-9")],
  ])("is dropped: %s", async (_what, leave) => {
    for (const key of [
      "GET /event-triggers/t-2",
      "GET /event-triggers/t-2/starts",
      "GET /activations/a-new",
    ]) {
      const f = setup();
      f.platform.starts.set("t-2", [requested("s-1")]);
      const request = f.platform.request.bind(f.platform);
      f.platform.request = async <T>(path: string, method = "GET") => {
        const answer = request<T>(path, method);
        if (`GET ${path.split("?")[0]!.replace(ENV, "")}` === key)
          leave(f.target.now!, f.platform);
        return answer;
      };
      await f.show();
      expect(kind(f), key).toBe("loading");
      expect(f.list.owed(), key).toBeNull();
      // Nothing more is asked of what they left.
      if (key === "GET /event-triggers/t-2")
        expect(f.platform.sent).toHaveLength(1);
    }
  });

  it("is read again for what they chose", async () => {
    const f = setup([CURRENT, { ...OLD, id: "t-9", name: "notify-team-9" }]);
    await f.show();
    f.target.now = { ...f.target.now!, triggerId: "t-9" };
    await f.show();
    expect(listed(f).trigger.name).toBe("notify-team-9");
  });

  it("ends when the account signs out", async () => {
    const f = setup();
    await f.show();
    f.target.now = null;
    await f.show();
    expect(kind(f)).toBe("offline");
  });
});

describe("a stop Studio still owes", () => {
  const owing = (seeds: Seed[] = [CURRENT, OLD], stops = [OWED]) =>
    setup(seeds, { pendingStops: stops });
  const FOR = "Two triggers start notify-team for the same events.";

  it("is the first thing said, naming both triggers, with Stop it", async () => {
    const f = owing();
    await f.show();
    expect(callout(f)).toEqual({
      tone: "warning",
      lead: FOR,
      detail: "Stop notify-team to keep only notify-team-2.",
      actions: ["stop"],
      busy: false,
    });
  });

  it.each<[string, Seed[], PendingStop[]]>([
    ["was stopped elsewhere", [CURRENT, { ...OLD, disabled: true }], [OWED]],
    ["is gone", [CURRENT], [OWED]],
    ["changed since", [CURRENT, { ...OLD, revision: 4 }], [OWED]],
    ["moved since", [CURRENT, { ...OLD, activation: "a-older" }], [OWED]],
    [
      "is another workflow's",
      [CURRENT, { ...OLD, activation: "a-other" }],
      [{ ...OWED, activationId: "a-other" }],
    ],
    [
      "is the one that stays",
      [CURRENT],
      [{ triggerId: "t-2", revision: 1, activationId: "a-new" }],
    ],
    [
      "would leave nothing reacting",
      [{ ...CURRENT, disabled: true }, OLD],
      [OWED],
    ],
  ])("isn't said of a trigger that %s", async (_what, seeds, stops) => {
    const f = owing(seeds, stops);
    await f.show();
    expect(f.list.owed()).toBeNull();
  });

  it("isn't said of a trigger whose activation runs another workflow", async () => {
    const f = owing();
    f.platform.runs.set("a-old", "sync-references");
    await f.show();
    expect(f.list.owed()).toBeNull();
  });

  it("offers no Stop it for a trigger it can't read", async () => {
    const f = owing();
    f.platform.refuse.set(
      "GET /event-triggers/t-1",
      refusal(503, "WV-UNAVAILABLE", "Try again later."),
    );
    await f.show();
    expect(callout(f)).toEqual({
      tone: "warning",
      lead: FOR,
      detail: "Stop the extra one when you next activate.",
      actions: [],
      busy: false,
    });
  });

  it("is made with the revision it showed, and no longer owed", async () => {
    const f = owing();
    await f.show();
    const stopping = f.list.stop();
    expect(callout(f)).toMatchObject({
      lead: FOR,
      detail: "Stopping notify-team…",
      actions: ["stop"],
      busy: true,
    });
    await stopping;
    expect(f.platform.sent.at(-1)).toMatchObject({
      key: "POST /event-triggers/t-1/disable",
      revision: 3,
    });
    expect(f.settled).toEqual([["env-1", "t-1"]]);
    expect(callout(f)).toEqual({
      tone: "success",
      lead: "Stopped notify-team.",
      detail: "Only notify-team-2 starts notify-team now.",
      actions: [],
      busy: false,
    });
    await f.list.refresh();
    expect(f.list.owed()).toBeNull();
  });

  it("is sent once, however often it is pressed", async () => {
    const f = owing();
    await f.show();
    await Promise.all([f.list.stop(), f.list.stop(), f.list.stopNow()]);
    expect(f.platform.writes()).toEqual(["POST /event-triggers/t-1/disable"]);
  });

  it("asks before it skips waiting events; Keep it for now brings the warning back, Stop now stops", async () => {
    const f = owing();
    f.platform.starts.set("t-1", [
      requested("s-1"),
      requested("s-2"),
      requested("s-3"),
    ]);
    await f.show();
    await f.list.stop();
    expect(callout(f)).toEqual({
      tone: "warning",
      lead: "notify-team has 3 events waiting to start. Stopping it now skips them.",
      detail:
        "Keep it for now and both start notify-team for each event they share until you stop it.",
      actions: ["keep", "now"],
      busy: false,
    });
    f.list.keep();
    expect(callout(f)).toMatchObject({ lead: FOR, actions: ["stop"] });
    expect(f.platform.writes()).toEqual([]);
    expect(f.settled).toEqual([]);
    await f.list.stop();
    await f.list.stopNow();
    expect(callout(f)).toMatchObject({ lead: "Stopped notify-team." });
  });

  it("asks the same when it can't check for waiting events", async () => {
    const f = owing();
    await f.show();
    f.platform.refuse.set(
      "GET /event-triggers/t-1/starts",
      refusal(403, "WV-FORBIDDEN", "Access denied"),
    );
    await f.list.stop();
    expect(callout(f)).toEqual({
      tone: "warning",
      lead: "Studio can't check whether notify-team has events waiting to start.",
      detail: "Stopping it now skips any that are waiting.",
      actions: ["keep", "now"],
      busy: false,
    });
  });

  it("stops nothing that changed since it was shown", async () => {
    const f = owing();
    await f.show();
    f.platform.find("t-1")!["revision"] = 4;
    await f.list.stop();
    expect(callout(f)).toEqual({
      tone: "warning",
      lead: "notify-team changed since you opened this. Review it.",
      detail:
        "Studio stopped nothing. Refresh the starts to see what it is now.",
      actions: ["refresh"],
      busy: false,
    });
    expect(f.platform.writes()).toEqual([]);
    expect(f.settled).toEqual([["env-1", "t-1"]]);
  });

  it.each([
    ["no longer reacts", { disabled: true }],
    ["moved to another version", { revision: 2 }],
    ["starts another workflow", { workflow_name: "sync-references" }],
  ])("stops nothing when the one that stays %s", async (_what, change) => {
    const f = owing();
    await f.show();
    Object.assign(f.platform.find("t-2")!, change);
    await f.list.stop();
    expect(callout(f)).toMatchObject({
      lead: "notify-team-2 changed since you opened this. Review it.",
      actions: ["refresh"],
    });
    expect(f.platform.writes()).toEqual([]);
    expect(f.settled).toEqual([]);
  });

  it("says why it couldn't, with Stop it again", async () => {
    const f = owing();
    await f.show();
    f.platform.refuse.set(
      "POST /event-triggers/t-1/disable",
      refusal(503, "WV-UNAVAILABLE", "the platform didn't answer in time"),
    );
    await f.list.stop();
    expect(callout(f)).toEqual({
      tone: "danger",
      lead: "Couldn't stop notify-team: the platform didn't answer in time.",
      detail:
        "Both start notify-team for each event they share until you stop it.",
      actions: ["stop"],
      busy: false,
    });
    expect(f.settled).toEqual([]);
    await f.list.stop();
    expect(callout(f)).toMatchObject({ lead: "Stopped notify-team." });
  });

  it("says so when the platform can't be asked about the one that stays", async () => {
    const f = owing();
    await f.show();
    f.platform.refuse.set(
      "GET /event-triggers/t-2",
      refusal(503, "WV-UNAVAILABLE", "the platform didn't answer in time"),
    );
    await f.list.stop();
    expect(callout(f)).toMatchObject({
      tone: "danger",
      lead: "Couldn't stop notify-team: the platform didn't answer in time.",
    });
    expect(f.platform.writes()).toEqual([]);
  });

  it("says who can stop it when the person may not", async () => {
    const f = owing();
    await f.show();
    f.platform.capabilities.delete("trigger.manage");
    await f.list.stop();
    expect(callout(f)).toEqual({
      tone: "danger",
      lead: "Couldn't stop notify-team: you need to manage triggers in development.",
      detail: "Ask an administrator to stop it, or for the permission.",
      actions: [],
      busy: false,
    });
  });

  it("names how many are left when several are owed", async () => {
    const more = { ...OLD, id: "t-0", name: "notify-team-0" };
    const f = owing(
      [CURRENT, OLD, more],
      [OWED, { triggerId: "t-0", revision: 3, activationId: "a-old" }],
    );
    await f.show();
    expect(callout(f)).toMatchObject({
      detail: "Stop notify-team. Another one is left to stop after it.",
    });
    await f.list.stop();
    expect(callout(f)).toMatchObject({
      lead: FOR,
      detail: "Stop notify-team-0 to keep only notify-team-2.",
    });
    expect(f.settled).toEqual([["env-1", "t-1"]]);
  });

  const TWO: [Seed[], PendingStop[]] = [
    [CURRENT, OLD, { ...OLD, id: "t-0", name: "notify-team-0" }],
    [OWED, { triggerId: "t-0", revision: 3, activationId: "a-old" }],
  ];
  const ALSO = "Stop notify-team-0 to keep only notify-team-2.";

  it.each<[string, Partial<Seed>, string]>([
    [
      "reacts to other events",
      { events: ["transaction.completed"] },
      "Completed · from move-between-systems",
    ],
    [
      "reacts to other workflows",
      { name: "notify-team-0", sources: ["sync-references"] },
      "Needs attention · from sync-references",
    ],
  ])(
    "names the events a stop ends of a trigger that %s, and what still starts it after the stop",
    async (_what, other, ends) => {
      const old = { ...OLD, ...other };
      const lead = `Trigger ${old.name} also starts this workflow, for other events.`;
      const f = owing([CURRENT, old]);
      f.platform.starts.set("t-1", [requested("s-1")]);
      await f.show();
      expect(callout(f)).toMatchObject({
        lead,
        detail: `It reacts to ${ends}. Stop it, and those events no longer start this workflow.`,
      });
      const both =
        "both start this workflow, for different events, until you stop it.";
      const stopping = f.list.stop();
      expect(callout(f)).toMatchObject({ lead, busy: true });
      await stopping;
      expect(callout(f)).toMatchObject({
        detail: `Keep it for now and ${both}`,
      });
      f.platform.refuse.set(
        "POST /event-triggers/t-1/disable",
        refusal(503, "WV-UNAVAILABLE", "Try again later."),
      );
      await f.list.stopNow();
      expect(callout(f)).toMatchObject({ detail: `B${both.slice(1)}` });
      f.platform.starts.delete("t-1");
      await f.list.stop();
      expect(callout(f)).toMatchObject({
        lead: `Stopped ${old.name}.`,
        detail: "notify-team-2 still starts this workflow.",
      });
    },
  );

  it("says how many are left after the one that reacts to other events", async () => {
    const f = owing(
      [CURRENT, { ...OLD, events: ["transaction.completed"] }, TWO[0][2]!],
      TWO[1],
    );
    await f.show();
    expect(callout(f)).toMatchObject({
      detail:
        "It reacts to Completed · from move-between-systems. Stop it, and those events no longer start this workflow. Another one is left to stop after it.",
    });
  });

  it("is read again only once the stop that runs answers", async () => {
    const f = owing(...TWO);
    await f.show();
    const release = held(f, "POST /event-triggers/t-1/disable");
    const stopping = f.list.stop();
    await settledSoFar();
    const asked = f.platform.sent.length;
    await f.list.refresh();
    expect(f.platform.sent).toHaveLength(asked);
    expect(listed(f)).toMatchObject({ refreshing: false });
    expect(callout(f)).toMatchObject({ detail: "Stopping notify-team…" });
    release();
    await stopping;
    expect(callout(f)).toMatchObject({ lead: FOR, detail: ALSO });
    await f.list.stop();
    expect(callout(f)).toMatchObject({
      lead: "Stopped notify-team-0.",
      detail: "Only notify-team-2 starts notify-team now.",
    });
    expect(f.platform.enabled().map((item) => item["id"])).toEqual(["t-2"]);
  });

  it.each(["fails", "answers"] as const)(
    "waits for a refresh that runs and %s: nothing is stopped meanwhile",
    async (how) => {
      const f = owing(...TWO);
      f.platform.starts.set("t-1", [requested("s-1")]);
      await f.show();
      f.platform.starts.delete("t-1");
      const release = held(f, "GET /event-triggers/t-2");
      if (how === "fails")
        f.platform.refuse.set(
          "GET /event-triggers/t-2/starts",
          refusal(503, "WV-UNAVAILABLE", "Try again later."),
        );
      const refreshed = f.list.refresh();
      await settledSoFar();
      const asked = f.platform.sent.length;
      await f.list.stop();
      expect(f.platform.sent).toHaveLength(asked);
      expect(callout(f)).toMatchObject({ lead: FOR, busy: false });
      release();
      await refreshed;
      if (how === "fails") {
        expect(kind(f)).toBe("failed");
        expect(f.list.owed()).toBeNull();
        f.wait(5_000);
        await f.list.refresh();
      }
      // Both are still owed, and each stop says what is left.
      await f.list.stop();
      expect(callout(f)).toMatchObject({ lead: FOR, detail: ALSO });
      await f.list.stop();
      expect(callout(f)).toMatchObject({ lead: "Stopped notify-team-0." });
      expect(f.platform.enabled().map((item) => item["id"])).toEqual(["t-2"]);
    },
  );

  it("skips no waiting event while a refresh runs", async () => {
    const f = owing();
    f.platform.starts.set("t-1", [requested("s-1")]);
    await f.show();
    await f.list.stop();
    expect(callout(f)).toMatchObject({ actions: ["keep", "now"] });
    const release = held(f, "GET /event-triggers/t-2");
    const refreshed = f.list.refresh();
    await settledSoFar();
    await f.list.stopNow();
    expect(f.platform.writes()).toEqual([]);
    release();
    await refreshed;
    expect(callout(f)).toMatchObject({ lead: FOR, actions: ["stop"] });
  });

  it("says nothing of a stop that answers after a newer read found nothing owed", async () => {
    const f = owing();
    await f.show();
    const release = held(f, "POST /event-triggers/t-1/disable");
    const stopping = f.list.stop();
    await settledSoFar();
    const here = f.target.now;
    f.target.now = null;
    await f.show();
    f.target.now = here;
    await f.show();
    expect(f.list.owed()).toBeNull();
    release();
    await stopping;
    expect(f.list.owed()).toBeNull();
  });

  it("takes nothing off the list the person looks at when it answers after they moved to another account", async () => {
    const f = owing(...TWO);
    await f.show();
    const release = held(f, "POST /event-triggers/t-1/disable");
    const stopping = f.list.stop();
    await settledSoFar();
    f.platform.find("t-1")!["disabled"] = false;
    f.platform.find("t-1")!["revision"] = 3;
    f.target.now = {
      ...f.target.now!,
      scope: "person-2",
      pendingStops: [TWO[1][1]!, OWED],
    };
    await f.show();
    expect(callout(f)).toMatchObject({
      detail: "Stop notify-team-0. Another one is left to stop after it.",
    });
    release();
    await stopping;
    expect(callout(f)).toMatchObject({
      detail: "Stop notify-team-0. Another one is left to stop after it.",
    });
    await f.list.stop();
    expect(callout(f)).toMatchObject({
      lead: FOR,
      detail: "Stop notify-team to keep only notify-team-2.",
    });
  });

  it("keeps what a newer read shows when the person left and came back while it ran", async () => {
    const f = owing();
    await f.show();
    const release = held(f, "POST /event-triggers/t-1/disable");
    const stopping = f.list.stop();
    await settledSoFar();
    f.platform.find("t-1")!["disabled"] = false;
    f.platform.find("t-1")!["revision"] = 3;
    const here = f.target.now;
    f.target.now = null;
    await f.show();
    f.target.now = here;
    await f.show();
    release();
    await stopping;
    expect(callout(f)).toMatchObject({ lead: FOR, actions: ["stop"] });
  });

  const LEFT: [string, (f: ReturnType<typeof setup>) => void][] = [
    [
      "another environment",
      (f) => (f.platform.environment = `${PROJECT}/environments/other`),
    ],
    [
      "another account",
      (f) => (f.target.now = { ...f.target.now!, scope: "person-2" }),
    ],
    [
      "another workflow",
      (f) => (f.target.now = { ...f.target.now!, workflow: "sync-references" }),
    ],
    ["no environment", (f) => (f.target.now = null)],
  ];
  it.each(LEFT)(
    "sends nothing for what the person left before it is drawn again: %s",
    async (_what, leave) => {
      const f = owing();
      await f.show();
      const asked = f.platform.sent.length;
      leave(f);
      await f.list.stop();
      expect(f.platform.sent).toHaveLength(asked);
      expect(f.settled).toEqual([]);
    },
  );
  it.each(LEFT)(
    "sends no stop once the person left while it checked: %s",
    async (_what, leave) => {
      const f = owing();
      await f.show();
      const release = held(f, "GET /event-triggers/t-2");
      const stopping = f.list.stop();
      await settledSoFar();
      const here = f.target.now;
      const environment = f.platform.environment;
      leave(f);
      release();
      await stopping;
      expect(f.platform.writes()).toEqual([]);
      expect(f.settled).toEqual([]);
      // Back before it was drawn: it is still owed, and Stop it works.
      f.target.now = here;
      f.platform.environment = environment;
      await f.list.stop();
      expect(callout(f)).toMatchObject({ lead: "Stopped notify-team." });
    },
  );

  it("is still cleared for the environment it was made in when the person chooses another meanwhile", async () => {
    const f = owing();
    await f.show();
    f.platform.before.set("POST /event-triggers/t-1/disable", () => {
      f.target.now = {
        ...f.target.now!,
        environmentId: "env-2",
        scope: "elsewhere",
      };
    });
    await f.list.stop();
    expect(f.settled).toEqual([["env-1", "t-1"]]);
    expect(f.list.owed()).toMatchObject({ kind: "stopping" });
  });
});
