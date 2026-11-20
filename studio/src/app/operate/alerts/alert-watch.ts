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
import { readAlertPage } from "./alerts-api";

/** What the watcher reads through: the shell's API and what the person may read. */
export interface AlertWatchHost {
  /** The person holds alert.read somewhere in the workspace. */
  readable(): boolean;
  /** The environment's API path, or "" without one. */
  environment(): string;
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
  private environment = "";
  constructor(private readonly host: AlertWatchHost) {}

  clear() {
    this.environment = "";
    this.badge.set(null);
  }
  /** Reads the counts again; throws when the read fails, so polling waits longer. */
  async load(): Promise<void> {
    const environment = this.host.readable() ? this.host.environment() : "";
    if (!environment) return this.clear();
    if (environment !== this.environment) this.badge.set(null);
    this.environment = environment;
    const answer = await this.host.request(
      `${environment}/alerts?state=firing&limit=1`,
    );
    if (
      environment !== this.environment ||
      environment !== this.host.environment()
    )
      return;
    try {
      const page = readAlertPage(answer);
      this.badge.set(alertBadge(page.firing, page.critical));
    } catch (error) {
      this.badge.set(null);
      throw error;
    }
  }
}
