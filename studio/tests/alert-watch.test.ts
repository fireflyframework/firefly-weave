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
// The navigation badge for Alerts: one read of the firing alerts (limit 1)
// gives the firing and critical counts; the bell rings and the badge turns
// red exactly while a critical alert fires; nothing is read without
// alert.read; and an answer for another environment never lands.
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../src/app/api";
import {
  AlertWatch,
  alertBadge,
  type AlertWatchHost,
} from "../src/app/operate/alerts/alert-watch";
import * as fixture from "./alerts-fixture";

function host(readable = true) {
  const asked: string[] = [];
  let answer: (value: unknown) => void = () => {};
  let fail: (error: unknown) => void = () => {};
  const where = { environment: "/env/a", readable, person: "ana" };
  const watchHost: AlertWatchHost = {
    readable: () => where.readable,
    environment: () => where.environment,
    scope: () => where.person,
    request: (path) => {
      asked.push(path);
      return new Promise((resolve, reject) => {
        answer = resolve;
        fail = reject;
      });
    },
  };
  return {
    watchHost,
    asked,
    where,
    answer: (value: unknown) => answer(value),
    fail: (error: unknown) => fail(error),
  };
}
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

describe("the Alerts badge", () => {
  it("the bell shows the firing count and rings while one is critical", () => {
    expect(alertBadge(2, 1)).toEqual({
      count: 2,
      critical: true,
      icon: "alertFiring",
      name: "Alerts, 2 firing, 1 critical",
    });
    expect(alertBadge(1, 0)).toEqual({
      count: 1,
      critical: false,
      icon: "alerts",
      name: "Alerts, 1 firing",
    });
    expect(alertBadge(0, 0)).toBeNull();
  });
  it("reads one firing alert and keeps both counts", async () => {
    const h = host();
    const watch = new AlertWatch(h.watchHost);
    const load = watch.load();
    expect(h.asked).toEqual(["/env/a/alerts?state=firing&limit=1"]);
    h.answer(fixture.alertPage(fixture.firing().slice(0, 1), 2, 1));
    await load;
    expect(watch.badge()).toMatchObject({ count: 2, critical: true });
  });
  it("no badge request without alert.read", async () => {
    const h = host(false);
    const watch = new AlertWatch(h.watchHost);
    await watch.load();
    expect(h.asked).toEqual([]);
    expect(watch.badge()).toBeNull();
  });
  it("drops an answer for an environment the person left", async () => {
    const h = host();
    const watch = new AlertWatch(h.watchHost);
    const load = watch.load();
    h.where.environment = "/env/b";
    h.answer(fixture.alertPage(fixture.firing(), 2, 1));
    await load;
    await settle();
    expect(watch.badge()).toBeNull();
  });
  it("drops an answer for a person who is no longer signed in", async () => {
    const h = host();
    const watch = new AlertWatch(h.watchHost);
    const load = watch.load();
    h.where.person = "ben";
    h.answer(fixture.alertPage(fixture.firing(), 2, 1));
    await load;
    expect(watch.badge()).toBeNull();
  });
  it("after a refusal, reads again only for another person or environment", async () => {
    const h = host();
    const watch = new AlertWatch(h.watchHost);
    const refused = watch.load();
    h.fail(new ApiError(403, { code: "WV-FORBIDDEN" }));
    await refused;
    await watch.load();
    await watch.load();
    expect(h.asked).toHaveLength(1);
    expect(watch.badge()).toBeNull();
    h.where.environment = "/env/b";
    const other = watch.load();
    h.answer(fixture.alertPage(fixture.firing(), 2, 1));
    await other;
    expect(h.asked).toEqual([
      "/env/a/alerts?state=firing&limit=1",
      "/env/b/alerts?state=firing&limit=1",
    ]);
    expect(watch.badge()).toMatchObject({ count: 2 });
  });
  it("throws for an answer it can't read, so polling waits longer", async () => {
    const h = host();
    const watch = new AlertWatch(h.watchHost);
    const load = watch.load();
    h.answer({ items: [], next_cursor: null });
    await expect(load).rejects.toThrow();
    expect(watch.badge()).toBeNull();
  });
  it("asks nothing before the environment is known", async () => {
    const h = host();
    const watch = new AlertWatch({
      ...h.watchHost,
      environment: () => {
        throw new Error("Choose an environment.");
      },
    });
    await watch.load();
    expect(h.asked).toEqual([]);
    expect(watch.badge()).toBeNull();
  });
  it("reads within 2 s of the person signing in, and not before", async () => {
    vi.useFakeTimers();
    try {
      const h = host(false);
      const quiet = {
        now: () => 0,
        setTimeout: () => 0,
        clearTimeout: () => undefined,
        visible: () => true,
        onVisibilityChange: () => () => undefined,
      };
      const watch = new AlertWatch(h.watchHost, quiet);
      watch.start();
      await vi.advanceTimersByTimeAsync(4000);
      expect(h.asked).toEqual([]);
      h.where.readable = true;
      await vi.advanceTimersByTimeAsync(2000);
      expect(h.asked).toEqual(["/env/a/alerts?state=firing&limit=1"]);
      watch.stop();
    } finally {
      vi.useRealTimers();
    }
  });
});
