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
// The Alerts page's reads and changes: each takes note of the environment,
// the person and its generation before it waits, and its answer lands only
// while all three hold; one section's failure leaves the others usable; and
// the page reads every 30 s while visible, waiting longer after failures.
import { describe, expect, it } from "vitest";
import { ApiError } from "../src/app/api";
import type { AlertsTransport } from "../src/app/operate/alerts/alerts-api";
import {
  AlertsStore,
  type AlertsHost,
} from "../src/app/operate/alerts/alerts-store";
import type { PollEnvironment } from "../src/app/operate/operate-store";
import * as fixture from "./alerts-fixture";

interface Pending {
  environment: string;
  path: string;
  method: string;
  resolve(value: unknown): void;
  reject(error: unknown): void;
}
const base = (environment: string) =>
  `/studio/api/api/v1/tenants/t/projects/p/environments/${environment}`;

/** A host whose answers wait until the test settles them. */
function platform() {
  const pending: Pending[] = [];
  const where = { environment: "a", person: "ana" };
  const api = (environment: string): AlertsTransport => ({
    environment: base(environment),
    project: "/studio/api/api/v1/tenants/t/projects/p",
    request<T>(path: string, method = "GET") {
      return new Promise<T>((resolve, reject) =>
        pending.push({
          environment,
          path,
          method,
          resolve: resolve as (value: unknown) => void,
          reject,
        }),
      );
    },
  });
  const host: AlertsHost = {
    api: () => api(where.environment),
    scope: () => JSON.stringify(where),
  };
  /** Settles the waiting requests whose path contains `part`. */
  const answer = (part: string, value: unknown, environment = "a") => {
    for (const request of pending.filter(
      (item) => item.environment === environment && item.path.includes(part),
    )) {
      pending.splice(pending.indexOf(request), 1);
      request.resolve(value);
    }
  };
  const fail = (part: string, error: unknown, environment = "a") => {
    for (const request of pending.filter(
      (item) => item.environment === environment && item.path.includes(part),
    )) {
      pending.splice(pending.indexOf(request), 1);
      request.reject(error);
    }
  };
  /** Answers everything waiting in an environment with the fixture. */
  const answerAll = (environment = "a") => {
    answer(
      "/alerts?state=firing",
      fixture.alertPage(fixture.firing()),
      environment,
    );
    answer(
      "/alerts?state=resolved",
      fixture.alertPage(fixture.resolved()),
      environment,
    );
    answer(
      "/alert-rules",
      { items: fixture.rules(), next_cursor: null },
      environment,
    );
    answer(
      "/alert-channels",
      { items: fixture.channels(), next_cursor: null },
      environment,
    );
    answer("/alert-presets", fixture.presets(), environment);
    answer("/alert-settings", fixture.settings(), environment);
    answer("/subscriptions", fixture.subscriptions(), environment);
    answer("/operations/summary", { health: [] }, environment);
  };
  return { host, where, pending, answer, fail, answerAll };
}
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
const now = () => Date.parse(fixture.NOW);
const draft = {
  name: "x",
  enabled: true,
  signal: "health.degraded",
  selector: {},
  comparator: "gt" as const,
  threshold: 0,
  window_seconds: null,
  for_seconds: 0,
  min_samples: 1,
  severity: "info" as const,
  channel_ids: [],
  renotify_seconds: 0,
};

describe("answers land only where they were asked", () => {
  it("drops rules, channels and settings read for another environment or person", async () => {
    for (const [part, section, move] of [
      ["/alert-rules", "rules", "environment"],
      ["/alert-channels", "channels", "environment"],
      ["/alert-settings", "settings", "person"],
    ] as const) {
      const p = platform();
      const store = new AlertsStore(p.host, { now });
      const load = store.load().catch(() => undefined);
      if (move === "environment") p.where.environment = "b";
      else p.where.person = "ben";
      store.forget();
      p.answerAll("a");
      await load;
      expect(store[section]().kind, part).toBe("idle");
      expect(store.alerts().kind, part).toBe("idle");
    }
  });
  it("keeps the newer load's alerts when an older answer arrives late", async () => {
    const p = platform();
    const store = new AlertsStore(p.host, { now });
    const first = store.load().catch(() => undefined);
    const older = p.pending.filter((item) =>
      item.path.includes("state=firing"),
    );
    p.pending.splice(p.pending.indexOf(older[0]), 1);
    const second = store.load();
    p.answerAll("a");
    await second;
    expect(store.alerts()).toMatchObject({
      kind: "ready",
      value: { firingCount: 2, critical: 1 },
    });
    older[0].resolve(fixture.alertPage([], 0, 0));
    await first;
    expect(store.alerts()).toMatchObject({ value: { firingCount: 2 } });
  });
  it("drops a preview asked in another environment", async () => {
    const p = platform();
    const store = new AlertsStore(p.host, { now });
    const preview = store.runPreview(draft);
    expect(store.preview().kind).toBe("loading");
    p.where.environment = "b";
    store.forget();
    p.answer("/alert-rules/preview", fixture.previewValue());
    expect(await preview).toBe("dropped");
    expect(store.preview().kind).toBe("idle");
  });
  it("drops an acknowledgement that answers after the environment changed", async () => {
    const p = platform();
    const store = new AlertsStore(p.host, { now });
    const load = store.load();
    p.answerAll("a");
    await load;
    const acknowledged = store.acknowledge(fixture.ids.firingRate);
    p.where.environment = "b";
    store.forget();
    const other = store.load();
    p.answer(
      "/alerts?state=firing",
      fixture.alertPage([fixture.firing()[0]], 1, 1),
      "b",
    );
    p.answerAll("b");
    await other;
    p.answer("/acknowledge", {
      ...fixture.firing()[0],
      acknowledged_at: fixture.NOW,
      acknowledged_by: fixture.ME,
    });
    expect(await acknowledged).toBe("dropped");
    const shown = store.alerts();
    expect(
      shown.kind === "ready" && shown.value.firing[0].acknowledged_at,
    ).toBeNull();
  });
  it("shows an acknowledgement that answers in the same environment", async () => {
    const p = platform();
    const store = new AlertsStore(p.host, { now });
    const load = store.load();
    p.answerAll("a");
    await load;
    const acknowledged = store.acknowledge(fixture.ids.firingRate);
    p.answer("/acknowledge", {
      ...fixture.firing()[0],
      acknowledged_at: fixture.NOW,
      acknowledged_by: fixture.ME,
    });
    expect(await acknowledged).toBe("applied");
    const shown = store.alerts();
    expect(
      shown.kind === "ready" && shown.value.firing[0].acknowledged_by,
    ).toBe(fixture.ME);
  });
});

describe("sections", () => {
  it("one section's error leaves the others usable", async () => {
    const p = platform();
    const store = new AlertsStore(p.host, { now });
    const load = store.load();
    p.fail(
      "/alert-rules",
      new ApiError(503, {
        code: "WV-UNAVAILABLE",
        message: "The platform didn't answer in time.",
      }),
    );
    p.answerAll("a");
    await expect(load).rejects.toThrow();
    expect(store.rules()).toMatchObject({
      kind: "error",
      error: {
        code: "WV-UNAVAILABLE",
        message: "The platform didn't answer in time.",
      },
    });
    expect(store.alerts().kind).toBe("ready");
    expect(store.channels().kind).toBe("ready");
    expect(store.webhooks()).toBe(2);
  });
  it("reads recent alerts from the last 7 days", async () => {
    const p = platform();
    const store = new AlertsStore(p.host, { now });
    const load = store.load();
    const recent = p.pending.find((item) =>
      item.path.includes("state=resolved"),
    )!;
    expect(new URL(recent.path, "http://studio").searchParams.get("from")).toBe(
      "2026-11-11T10:54:10.000Z",
    );
    p.answerAll("a");
    await load;
    const shown = store.alerts();
    expect(shown.kind === "ready" && shown.value.recent.length).toBe(3);
  });
  it("shows a refusal as the section's access state, not an error", async () => {
    const p = platform();
    const store = new AlertsStore(p.host, { now });
    const load = store.load();
    p.fail("/subscriptions", new ApiError(403, { code: "WV-FORBIDDEN" }));
    p.fail("/alert-settings", new ApiError(403, { code: "WV-FORBIDDEN" }));
    p.answerAll("a");
    // A refusal isn't a failure: the next read doesn't wait longer.
    await expect(load).resolves.toBe(true);
    expect(store.webhooks()).toBeNull();
    expect(store.settings().kind).toBe("refused");
    expect(store.rules().kind).toBe("ready");
  });
});

describe("polling", () => {
  it("stops polling while hidden and backs off on errors", async () => {
    const timers: { ms: number; run: () => void; live: boolean }[] = [];
    let visible = false;
    let listener = () => {};
    const env: PollEnvironment = {
      now,
      setTimeout: (run, ms) => timers.push({ ms, run, live: true }),
      clearTimeout: (handle) => {
        if (timers[handle - 1]) timers[handle - 1].live = false;
      },
      visible: () => visible,
      onVisibilityChange: (change) => {
        listener = change;
        return () => {
          listener = () => {};
        };
      },
    };
    const p = platform();
    const store = new AlertsStore(p.host, { now, env });
    store.poller.start(false);
    expect(timers.filter((timer) => timer.live)).toHaveLength(0);
    visible = true;
    listener();
    expect(p.pending.length).toBeGreaterThan(0);
    p.fail(
      "/alerts?state=firing",
      new ApiError(503, { code: "WV-UNAVAILABLE" }),
    );
    p.answerAll("a");
    await settle();
    await settle();
    const live = timers.filter((timer) => timer.live);
    expect(live.map((timer) => timer.ms)).toEqual([60_000]);
    visible = false;
    listener();
    expect(timers.filter((timer) => timer.live)).toHaveLength(0);
    store.poller.stop();
  });
});
