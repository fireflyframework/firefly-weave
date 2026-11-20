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
// The reads and changes behind Operate › Alerts. Each one takes note of the
// environment, the person and the view's generation before its first await,
// and its answer lands only while all three still hold: an environment
// switch, another person or a newer load drops it. Each section (alerts,
// rules, channels, settings) succeeds or fails on its own, so one tab's error
// leaves the others usable. State is held in signals, so OnPush templates
// follow it. The page reads every 30 s while visible, waiting twice as long
// after a failure, at most 60 s.
import { signal, type WritableSignal } from "@angular/core";
import { describeError, type PlainError } from "../../errors";
import {
  Poller,
  browserEnvironment,
  type PollEnvironment,
} from "../operate-store";
import {
  acknowledgeAlert,
  countAlertWebhooks,
  createChannel,
  createRule,
  deleteChannel,
  deleteRule,
  listAlerts,
  listChannels,
  listPresets,
  listRules,
  previewRule,
  readAlertSettings,
  testChannel,
  updateChannel,
  updateRule,
  type Alert,
  type AlertChannel,
  type AlertChannelDraft,
  type AlertNotification,
  type AlertPreset,
  type AlertPreview,
  type AlertRule,
  type AlertRuleDraft,
  type AlertSettings,
  type AlertsTransport,
} from "./alerts-api";

/** How often the page reads while visible: the rules are checked every 30 s. */
export const ALERTS_POLL_MS = 30_000;
/** How far back Recent reaches. */
const RECENT_MS = 7 * 86_400_000;

/** What the store reads through: the current platform and whose view it is. */
export interface AlertsHost {
  /** The platform's API; its environment may be unknown (it throws). */
  api(): AlertsTransport;
  /** The environment, person and sign-in: a change means a new view. */
  scope(): string;
}
/** One section of the page: never read, reading, read, failed, or refused (403). */
export type Section<T> =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "ready"; value: T }
  | { kind: "error"; error: PlainError }
  | { kind: "refused" };
export interface AlertsData {
  firing: Alert[];
  recent: Alert[];
  firingCount: number;
  critical: number;
}
export type PreviewState =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "ready"; preview: AlertPreview; draft: AlertRuleDraft }
  | { kind: "error"; error: PlainError };
/** What a read or change did: its answer landed, was dropped, or it failed. */
export type ReadResult = "applied" | "dropped" | "failed";

function environmentOf(host: AlertsHost): string {
  try {
    return host.api().environment;
  } catch {
    return "";
  }
}
const refused = (error: unknown) => describeError(error).status === 403;

/** Whether the summary says the alert checker is degraded or down. */
async function evaluatorDown(api: AlertsTransport): Promise<boolean> {
  const summary = await api.request<unknown>(
    `${api.environment}/operations/summary`,
  );
  const health =
    summary && typeof summary === "object" && "health" in summary
      ? (summary as { health: unknown }).health
      : [];
  return (
    Array.isArray(health) &&
    health.some(
      (item) =>
        !!item &&
        typeof item === "object" &&
        (item as Record<string, unknown>)["component"] === "alert_evaluator" &&
        ["degraded", "down"].includes(
          String((item as Record<string, unknown>)["state"]),
        ),
    )
  );
}

export class AlertsStore {
  readonly alerts = signal<Section<AlertsData>>({ kind: "idle" });
  readonly rules = signal<Section<AlertRule[]>>({ kind: "idle" });
  readonly channels = signal<Section<AlertChannel[]>>({ kind: "idle" });
  readonly settings = signal<Section<AlertSettings>>({ kind: "idle" });
  readonly presets = signal<AlertPreset[]>([]);
  /** Webhook subscriptions that receive alert changes; null when unknown. */
  readonly webhooks = signal<number | null>(null);
  /** The summary says the alert checker isn't running. */
  readonly evaluatorDown = signal(false);
  readonly preview = signal<PreviewState>({ kind: "idle" });
  /** When a load last finished (epoch milliseconds). */
  readonly updatedAt = signal<number | null>(null);
  readonly poller: Poller;
  private readonly now: () => number;
  private readonly onFailure: (error: unknown) => void;
  /** A new view: forget() moves it on, and nothing older lands. */
  private epoch = 0;
  private loads = 0;
  private previews = 0;

  constructor(
    private readonly host: AlertsHost,
    options: {
      now?: () => number;
      env?: PollEnvironment;
      /** True while a form is open: scheduled reads wait. */
      paused?: () => boolean;
      /** Hears every failed read, so the shell can explain an ended sign-in. */
      onFailure?: (error: unknown) => void;
    } = {},
  ) {
    this.now = options.now ?? (() => Date.now());
    this.onFailure = options.onFailure ?? (() => undefined);
    this.poller = new Poller(
      () => this.load(),
      ALERTS_POLL_MS,
      options.env ?? browserEnvironment,
      options.paused ?? (() => false),
    );
  }

  /** A check that the view is still the one a request was made for. */
  private fence(): () => boolean {
    const epoch = this.epoch;
    const scope = this.host.scope();
    const environment = environmentOf(this.host);
    return () =>
      epoch === this.epoch &&
      scope === this.host.scope() &&
      environment === environmentOf(this.host);
  }

  /** Another environment or person: nothing read before stays. */
  forget() {
    this.epoch++;
    this.alerts.set({ kind: "idle" });
    this.rules.set({ kind: "idle" });
    this.channels.set({ kind: "idle" });
    this.settings.set({ kind: "idle" });
    this.presets.set([]);
    this.webhooks.set(null);
    this.evaluatorDown.set(false);
    this.preview.set({ kind: "idle" });
    this.updatedAt.set(null);
  }

  /**
   * Reads every section. Throws when a section failed, so polling waits
   * longer; the sections that answered still show.
   */
  async load(): Promise<boolean> {
    if (!environmentOf(this.host)) return false;
    const load = ++this.loads;
    const view = this.fence();
    const current = () => view() && load === this.loads;
    const api = this.host.api();
    for (const section of [
      this.alerts,
      this.rules,
      this.channels,
      this.settings,
    ])
      if (section().kind === "idle") section.set({ kind: "loading" });
    // Whole seconds, so a cursor's query reads the same on every page.
    const from = new Date(Math.floor((this.now() - RECENT_MS) / 1000) * 1000)
      .toISOString()
      .replace(".000Z", "Z");
    const results = await Promise.all([
      this.read(this.alerts, current, async () => {
        const [firing, recent] = await Promise.all([
          listAlerts(api, { state: "firing", limit: 100 }),
          listAlerts(api, { state: "resolved", from, limit: 50 }),
        ]);
        return {
          firing: firing.items,
          recent: recent.items,
          firingCount: firing.firing,
          critical: firing.critical,
        };
      }),
      this.read(this.rules, current, () => listRules(api)),
      this.read(this.channels, current, () => listChannels(api)),
      this.read(this.settings, current, () => readAlertSettings(api)),
      this.extra(
        current,
        () => listPresets(api),
        (value) => this.presets.set(value),
      ),
      this.extra(
        current,
        () => countAlertWebhooks(api),
        (value) => this.webhooks.set(value),
        () => this.webhooks.set(null),
      ),
      this.extra(
        current,
        () => evaluatorDown(api),
        (value) => this.evaluatorDown.set(value),
        () => this.evaluatorDown.set(false),
      ),
    ]);
    if (!current()) return false;
    this.updatedAt.set(this.now());
    if (results.includes("failed"))
      throw new Error("Some alert sections could not be read.");
    return true;
  }

  private async read<T>(
    section: WritableSignal<Section<T>>,
    current: () => boolean,
    run: () => Promise<T>,
  ): Promise<ReadResult> {
    try {
      const value = await run();
      if (!current()) return "dropped";
      section.set({ kind: "ready", value });
      return "applied";
    } catch (error) {
      if (!current()) return "dropped";
      this.onFailure(error);
      if (refused(error)) {
        section.set({ kind: "refused" });
        return "applied";
      }
      section.set({ kind: "error", error: describeError(error) });
      return "failed";
    }
  }

  /** A read the page can do without: a failure keeps what it showed. */
  private async extra<T>(
    current: () => boolean,
    run: () => Promise<T>,
    apply: (value: T) => void,
    refusedRead: () => void = () => undefined,
  ): Promise<ReadResult> {
    try {
      const value = await run();
      if (!current()) return "dropped";
      apply(value);
      return "applied";
    } catch (error) {
      if (!current()) return "dropped";
      if (refused(error)) refusedRead();
      return "applied";
    }
  }

  /** Preview of a draft; a newer preview or another view drops the older answer. */
  async runPreview(draft: AlertRuleDraft): Promise<ReadResult> {
    const sequence = ++this.previews;
    const view = this.fence();
    const current = () => view() && sequence === this.previews;
    this.preview.set({ kind: "loading" });
    try {
      const preview = await previewRule(this.host.api(), draft);
      if (!current()) return "dropped";
      this.preview.set({ kind: "ready", preview, draft });
      return "applied";
    } catch (error) {
      if (!current()) return "dropped";
      this.onFailure(error);
      this.preview.set({ kind: "error", error: describeError(error) });
      return "failed";
    }
  }
  /** The draft changed: an older preview no longer describes it. */
  clearPreview() {
    this.previews++;
    this.preview.set({ kind: "idle" });
  }

  /**
   * Runs a change and applies its answer while the view still holds.
   * Resolves with the answer, or null when the view moved on meanwhile;
   * rejects with the platform's error otherwise. A read that started before
   * the change answered describes the platform before it, so its answer is
   * dropped and the page reads again.
   */
  private async change<T>(
    run: (api: AlertsTransport) => Promise<T>,
    apply: (value: T) => void,
  ): Promise<T | null> {
    const current = this.fence();
    try {
      const value = await run(this.host.api());
      if (!current()) return null;
      apply(value);
      this.loads++;
      void this.poller.refresh();
      return value;
    } catch (error) {
      if (!current()) return null;
      this.onFailure(error);
      throw error;
    }
  }

  /** Stops repeat notifications; the alert shows acknowledged at once. */
  async acknowledge(alertId: string): Promise<ReadResult> {
    try {
      const answer = await this.change(
        (api) => acknowledgeAlert(api, alertId),
        (alert) =>
          this.patch(this.alerts, (data) => ({
            ...data,
            firing: data.firing.map((item) =>
              item.id === alert.id ? alert : item,
            ),
          })),
      );
      return answer ? "applied" : "dropped";
    } catch {
      return "failed";
    }
  }

  /** Creates a rule (with an idempotency key) or saves one at its revision. */
  saveRule(
    draft: AlertRuleDraft,
    existing: { id: string; revision: number } | null,
    key: string,
  ): Promise<AlertRule | null> {
    return this.change(
      (api) =>
        existing
          ? updateRule(api, existing.id, existing.revision, draft)
          : createRule(api, draft, key),
      (rule) => this.putRule(rule),
    );
  }
  /** Switches a rule on or off, at the revision shown. */
  setEnabled(rule: AlertRule, enabled: boolean): Promise<AlertRule | null> {
    const draft: AlertRuleDraft = {
      name: rule.name,
      enabled,
      signal: rule.signal,
      selector: rule.selector,
      comparator: rule.comparator,
      threshold: rule.threshold,
      window_seconds: rule.window_seconds,
      for_seconds: rule.for_seconds,
      min_samples: rule.min_samples,
      severity: rule.severity,
      channel_ids: rule.channel_ids,
      renotify_seconds: rule.renotify_seconds,
    };
    return this.change(
      (api) => updateRule(api, rule.id, rule.revision, draft),
      (saved) => this.putRule(saved),
    );
  }
  removeRule(rule: AlertRule): Promise<true | null> {
    return this.change(
      async (api) => {
        await deleteRule(api, rule.id, rule.revision);
        return true as const;
      },
      () =>
        this.patch(this.rules, (rules) =>
          rules.filter((item) => item.id !== rule.id),
        ),
    );
  }
  private putRule(rule: AlertRule) {
    this.patch(this.rules, (rules) =>
      rules.some((item) => item.id === rule.id)
        ? rules.map((item) => (item.id === rule.id ? rule : item))
        : [...rules, rule],
    );
  }

  saveChannel(
    draft: AlertChannelDraft,
    existing: { id: string; revision: number } | null,
    key: string,
  ): Promise<AlertChannel | null> {
    return this.change(
      (api) =>
        existing
          ? updateChannel(api, existing.id, existing.revision, draft)
          : createChannel(api, draft, key),
      (channel) =>
        this.patch(this.channels, (channels) =>
          channels.some((item) => item.id === channel.id)
            ? channels.map((item) => (item.id === channel.id ? channel : item))
            : [...channels, channel],
        ),
    );
  }
  removeChannel(channel: AlertChannel): Promise<true | null> {
    return this.change(
      async (api) => {
        await deleteChannel(api, channel.id, channel.revision);
        return true as const;
      },
      () =>
        this.patch(this.channels, (channels) =>
          channels.filter((item) => item.id !== channel.id),
        ),
    );
  }
  sendTest(channelId: string, key: string): Promise<AlertNotification | null> {
    return this.change(
      (api) => testChannel(api, channelId, key),
      () => undefined,
    );
  }

  private patch<T>(section: WritableSignal<Section<T>>, edit: (value: T) => T) {
    const now = section();
    if (now.kind === "ready")
      section.set({ kind: "ready", value: edit(now.value) });
  }
}
