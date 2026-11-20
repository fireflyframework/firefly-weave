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
// The Alerts entry's badge in the navigation: how many alerts fire now and
// how many of them are critical, from one read of the firing alerts with
// limit 1, every 30 s while the page is visible and only with alert.read.
// The shell loads this file after the first page, so the first page doesn't
// grow. The bell rings and the badge turns red exactly while a critical
// alert fires.
import { signal } from "@angular/core";
import { describeError } from "../../errors";
import {
  Poller,
  browserEnvironment,
  type PollEnvironment,
} from "../operate-store";
import { readAlertPage } from "./alerts-api";

/** What the watcher reads through: the shell's API and what the person may read. */
export interface AlertWatchHost {
  /** The person holds alert.read somewhere in the workspace. */
  readable(): boolean;
  /** The environment's API path; "" or a throw without one. */
  environment(): string;
  /** Whose view it is: a change (another person signed in) reads again. */
  scope?(): unknown;
  request(path: string): Promise<unknown>;
}
export interface AlertBadge {
  count: number;
  critical: boolean;
  icon: "alerts" | "alertFiring";
  /** The entry's accessible name: "Alerts, 2 firing, 1 critical". */
  name: string;
}

/** The badge for these counts; null while nothing fires. */
export function alertBadge(
  firing: number,
  critical: number,
): AlertBadge | null {
  if (firing <= 0) return null;
  return {
    count: firing,
    critical: critical > 0,
    icon: critical > 0 ? "alertFiring" : "alerts",
    name: `Alerts, ${firing} firing${critical > 0 ? `, ${critical} critical` : ""}`,
  };
}

export class AlertWatch {
  readonly badge = signal<AlertBadge | null>(null);
  /** Reads every 30 s while the page is visible, waiting longer after failures. */
  readonly poller: Poller;
  private environment = "";
  constructor(
    private readonly host: AlertWatchHost,
    env: PollEnvironment = browserEnvironment,
  ) {
    this.poller = new Poller(() => this.load(), 30_000, env);
  }

  private watched: unknown[] = [];
  private follower: ReturnType<typeof setInterval> | null = null;
  /**
   * Reads now, then every 30 s; and within 2 s of the person signing in,
   * another person, or another environment, without a request until then.
   */
  start() {
    this.poller.start(true);
    this.watched = [this.where(), this.host.scope?.()];
    this.follower ??= setInterval(() => {
      const now = [this.where(), this.host.scope?.()];
      if (now.every((part, index) => part === this.watched[index])) return;
      this.watched = now;
      void this.refresh();
    }, 2000);
  }
  stop() {
    this.poller.stop();
    if (this.follower) clearInterval(this.follower);
    this.follower = null;
  }
  /** The person or environment changed: read again now. */
  refresh() {
    return this.poller.refresh();
  }
  private where() {
    if (!this.host.readable()) return "";
    try {
      return this.host.environment();
    } catch {
      return "";
    }
  }

  clear() {
    this.environment = "";
    this.badge.set(null);
  }
  /**
   * Where the platform refused the read (403): a person with alert.read on
   * some records only. The badge asks again for another person or environment.
   */
  private refusal: [string, unknown] | null = null;
  /** Reads the counts again; throws when the read fails, so polling waits longer. */
  async load(): Promise<void> {
    const environment = this.where();
    if (!environment) return this.clear();
    const scope = this.host.scope?.();
    if (this.refusal?.[0] === environment && this.refusal[1] === scope) return;
    this.refusal = null;
    if (environment !== this.environment) this.badge.set(null);
    this.environment = environment;
    const current = () =>
      environment === this.environment &&
      environment === this.where() &&
      scope === this.host.scope?.();
    let answer: unknown;
    try {
      answer = await this.host.request(
        `${environment}/alerts?state=firing&limit=1`,
      );
    } catch (error) {
      if (!current()) return;
      if (describeError(error).status !== 403) throw error;
      this.refusal = [environment, scope];
      this.badge.set(null);
      return;
    }
    if (!current()) return;
    try {
      const page = readAlertPage(answer);
      this.badge.set(alertBadge(page.firing, page.critical));
    } catch (error) {
      this.badge.set(null);
      throw error;
    }
  }
}

/** What the shell gives the watcher: its platform, the person and their grants. */
export interface AlertShell {
  readonly profile: unknown;
  readonly identity: unknown;
  readonly signInEnded: boolean;
  readonly api: {
    readonly environment: string;
    request<T>(path: string): Promise<T>;
  };
  canAnywhere(capability: string): boolean;
}
/** Starts the badge's reads for the shell, only while the person may read alerts. */
export function watchAlerts(shell: AlertShell): AlertWatch {
  const watch = new AlertWatch({
    readable: () =>
      !!shell.profile &&
      !!shell.identity &&
      !shell.signInEnded &&
      shell.canAnywhere("alert.read"),
    environment: () => shell.api.environment,
    scope: () => shell.identity,
    request: (path) => shell.api.request(path),
  });
  watch.start();
  return watch;
}
