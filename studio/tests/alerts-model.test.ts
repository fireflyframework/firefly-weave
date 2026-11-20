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
// What the Alerts page says about rules, alerts and recipients: percent
// thresholds both ways, every signal in words, each rule's condition and
// "Now", a firing alert's line and channels, how a resolved one ended, and
// the rule a form makes.
import { describe, expect, it } from "vitest";
import {
  alertSignals,
  readAlert,
  readRule,
  type Alert,
  type AlertRule,
} from "../src/app/operate/alerts/alerts-api";
import {
  SIGNAL_WORDS,
  alertLine,
  conditionText,
  emptyRuleForm,
  formFromRule,
  fromPercent,
  nameParts,
  notificationText,
  recipientProblem,
  resolvedText,
  ruleDraft,
  spanText,
  startedText,
  stateLine,
  toPercent,
} from "../src/app/operate/alerts/alerts-model";
import * as fixture from "./alerts-fixture";
import { checkAlertsWithPython, pythonAvailable } from "./alerts-oracle";

const now = new Date(fixture.NOW);
const rules = fixture.rules().map((item) => readRule(item) as AlertRule);
const firing = fixture.firing().map((item) => readAlert(item) as Alert);
const resolved = fixture.resolved().map((item) => readAlert(item) as Alert);
const names: Record<string, string> = {
  [fixture.ids.k3d]: "k3d-weave-test.team-a",
  [fixture.ids.kind]: "kind-weave-test",
};
const target = (id: string) => names[id] ?? "";

describe("percent thresholds", () => {
  it("a threshold of 10 % is sent as 0.1 and shown as 10 %", () => {
    expect(fromPercent("10")).toBe(0.1);
    expect(toPercent(0.1)).toBe("10 %");
    expect(fromPercent("3.2")).toBe(0.032);
    expect(toPercent(0.032)).toBe("3.2 %");
    // Two decimals: what the field shows is what is sent.
    expect(fromPercent("12.345")).toBe(0.1235);
    expect(toPercent(0.07)).toBe("7 %");
    expect(fromPercent(" 7 % ")).toBe(0.07);
  });
  it("refuses a percent that isn't a number from 0 to 100", () => {
    for (const text of ["", "abc", "-1", "100.5", "1e3", "Infinity"])
      expect(fromPercent(text), text).toBeNull();
    expect(fromPercent("100")).toBe(1);
    expect(fromPercent("0")).toBe(0);
  });
});

describe("signal words", () => {
  it("names every signal Studio knows, and an unknown one by its name", () => {
    expect(Object.keys(SIGNAL_WORDS)).toEqual([...alertSignals]);
    expect(SIGNAL_WORDS["run.failure_rate"]).toMatchObject({
      name: "Run failure rate",
      hint: "Percent of ended runs that failed or timed out.",
      unit: "ratio",
      window: true,
      sampled: true,
      selector: "workflow",
    });
    expect(SIGNAL_WORDS["targets.runner_offline"].selector).toBe("target_id");
    expect(SIGNAL_WORDS["health.degraded"].window).toBe(false);
  });
  it.skipIf(!pythonAvailable)(
    "every server signal has words, and the fixture keeps the contract",
    () => {
      const vocabularies = checkAlertsWithPython({
        AlertRuleView: fixture.rules(),
        AlertPage: [
          fixture.alertPage([...fixture.firing(), ...fixture.resolved()]),
        ],
        AlertChannel: fixture.channels(),
        AlertPreset: fixture.presets(),
        AlertSettings: [fixture.settings()],
        AlertRecipientConsent: fixture.consents(),
        AlertPreview: [fixture.previewNoData(), fixture.previewValue()],
        MyAlertAddress: [fixture.myAddress(), {}],
        RetentionPeriods: [fixture.retentionPeriods()],
      });
      expect(vocabularies["signals"]).toEqual([...alertSignals]);
      expect(Object.keys(SIGNAL_WORDS).sort()).toEqual(
        [...(vocabularies["signals"] as string[])].sort(),
      );
    },
  );
});

describe("a rule's condition and its Now", () => {
  it("says each rule's condition as the Rules tab shows it", () => {
    expect(rules.map(conditionText)).toEqual([
      "Above 0 · for 5 min",
      "Above 10 % over 15 min · at least 10 runs",
      "Below 1 · for 2 min",
      "Above 0 · for 2 min",
      "Above 300 s · for 2 min",
      "Above 30 s over 1 hour",
      "Above 0",
      "Above 0 · for 5 min",
    ]);
  });
  it("says where each rule stands, the ones not checked included", () => {
    const lines = rules.map((rule) =>
      stateLine(rule, {
        now,
        alert: firing.find((alert) => alert.id === rule.state.alert_id),
        target,
      }),
    );
    expect(lines.map((line) => line.text)).toEqual([
      "OK · 0",
      "Firing for 12 min · 14.6 %",
      "OK · 2",
      "Firing for 38 min · 1 · acknowledged",
      "OK · 42 s",
      "Last check failed 2 min ago",
      "Not checked: your access to kind-weave-test was removed",
      "Not checked: this server doesn't know the signal",
    ]);
    expect(lines.map((line) => line.tone)).toEqual([
      "success",
      "danger",
      "success",
      "danger",
      "success",
      "warning",
      "warning",
      "subtle",
    ]);
  });
  it("says a rule without data and one holding before it fires", () => {
    const base = rules[1];
    expect(
      stateLine(
        {
          ...base,
          state: { ...base.state, state: "ok", value: null, no_data: true },
        },
        { now, target },
      ).text,
    ).toBe("No data");
    expect(
      stateLine(
        {
          ...base,
          state: {
            ...base.state,
            state: "pending",
            since: "2026-11-18T10:53:00Z",
            alert_id: null,
          },
        },
        { now, target },
      ).text,
    ).toBe("Condition met for 1 min · 14.6 %");
  });
  it("names a removed target by its ID when its name can't be read", () => {
    expect(stateLine(rules[6], { now, target: () => "" }).text).toBe(
      "Not checked: your access to target f1000000 was removed",
    );
  });
});

describe("firing and resolved alerts", () => {
  it("says a firing alert's value against its threshold, its start and channels", () => {
    expect(firing.map((alert) => alertLine(alert))).toEqual([
      "Firing · 14.6 % > 10 %",
      "Firing · 1 > 0",
    ]);
    expect(startedText(firing[0].started_at, now, "UTC")).toBe("Today 10:42");
    expect(spanText(now.getTime() - Date.parse(firing[0].started_at))).toBe(
      "12 min",
    );
    expect(firing[0].notifications.map(notificationText)).toEqual([
      "Sent",
      "Failed: the mail server refused the message",
    ]);
    expect(firing[1].notifications.map(notificationText)).toEqual([
      "Skipped: hourly limit reached",
    ]);
  });
  it("says how each recent alert ended and how long it fired", () => {
    expect(
      resolved.map((alert) => resolvedText(alert.resolved_reason)),
    ).toEqual([
      "Back within the threshold",
      "Back within the threshold",
      "Rule disabled",
    ]);
    expect(
      resolved.map((alert) =>
        spanText(Date.parse(alert.resolved_at!) - Date.parse(alert.started_at)),
      ),
    ).toEqual(["9 min", "14 min", "2 h 10 min"]);
    expect(
      resolved.map((alert) => startedText(alert.started_at, now, "UTC")),
    ).toEqual(["Today 08:14", "Yesterday 22:40", "Yesterday 16:02"]);
    expect(startedText("2026-11-03T09:05:00Z", now, "UTC")).toBe(
      "Tue 3 Nov 09:05",
    );
  });
  it("says every way a notification can end", () => {
    const at = fixture.NOW;
    const words = (state: string, code: string | null) =>
      notificationText({
        channel_id: fixture.ids.team,
        kind: "firing",
        state: state as never,
        code,
        at,
      });
    expect(words("pending", null)).toBe("Pending");
    expect(words("pending", "mail_refused")).toBe(
      "Pending: the mail server refused the message, trying again",
    );
    expect(words("sent", "mail_refused")).toBe(
      "Sent to some recipients; the mail server refused the others",
    );
    expect(words("failed", "mail_unknown")).toBe(
      "Failed: the mail server didn't say whether it took the message",
    );
    expect(words("failed", "owner_unauthorized")).toBe(
      "Failed: the channel's owner can't send mail anymore",
    );
    expect(words("failed", "recipient_without_address")).toBe(
      "Failed: a member hasn't set an alert address",
    );
    expect(words("failed", "something_new")).toBe("Failed");
  });
});

describe("the rule a form makes", () => {
  const form = () => ({
    ...emptyRuleForm(),
    name: "invoice-approval failure rate above 10 %",
    signal: "run.failure_rate",
    selector: "invoice-approval",
    comparator: "gt",
    threshold: "10",
    window: "900",
    forSeconds: "0",
    minSamples: "10",
    severity: "warning",
    channels: [fixture.ids.oncall],
    renotify: "3600",
  });
  it("sends the percent as a ratio, with the workflow and the window", () => {
    expect(ruleDraft(form())).toEqual({
      draft: {
        name: "invoice-approval failure rate above 10 %",
        enabled: true,
        signal: "run.failure_rate",
        selector: { workflow: "invoice-approval" },
        comparator: "gt",
        threshold: 0.1,
        window_seconds: 900,
        for_seconds: 0,
        min_samples: 10,
        severity: "warning",
        channel_ids: [fixture.ids.oncall],
        renotify_seconds: 3600,
      },
    });
  });
  it("leaves a gauge without a window or a minimum of runs", () => {
    const result = ruleDraft({
      ...form(),
      signal: "queue.oldest_ready_seconds",
      selector: "",
      threshold: "300",
      window: "900",
      minSamples: "10",
    });
    expect(result).toMatchObject({
      draft: {
        signal: "queue.oldest_ready_seconds",
        selector: {},
        threshold: 300,
        window_seconds: null,
        min_samples: 1,
      },
    });
  });
  it("names each field it can't send", () => {
    expect(
      ruleDraft({ ...form(), name: " ", threshold: "120", minSamples: "0" }),
    ).toEqual({
      problems: {
        name: "Name the rule.",
        threshold: "Enter a percent from 0 to 100.",
        minSamples: "Enter a whole number from 1 to 10,000.",
      },
    });
    expect(
      ruleDraft({ ...form(), signal: "workers.online", threshold: "x" }),
    ).toEqual({
      problems: { threshold: "Enter a number from 0 to 1,000,000,000." },
    });
  });
  it("opens a saved rule with its percent and channels as they were", () => {
    const opened = formFromRule(rules[1]);
    expect(opened).toMatchObject({
      name: "Run failure rate above 10 %",
      threshold: "10",
      window: "900",
      minSamples: "10",
      channels: [fixture.ids.oncall, fixture.ids.team],
      renotify: "3600",
    });
    expect(ruleDraft(opened)).toMatchObject({
      draft: {
        threshold: 0.1,
        channel_ids: [fixture.ids.oncall, fixture.ids.team],
      },
    });
  });
});

describe("recipient refusals", () => {
  it("says why the platform refused an outside address", () => {
    expect(
      recipientProblem("domain_not_allowed", "alerts@example.org", "local"),
    ).toBe("example.org isn't an allowed email domain in local.");
    expect(recipientProblem("no_consent", "duty@vendor.example", "local")).toBe(
      "duty@vendor.example has no opt-in in local. An administrator can record one in Settings › Administration.",
    );
    expect(
      recipientProblem("consent_by_creator", "pager@vendor.example", "local"),
    ).toBe(
      "You recorded this opt-in, so another administrator adds the address.",
    );
    expect(recipientProblem("not_member", "", "local")).toBe(
      "This person isn't a member of the project anymore.",
    );
  });
});

describe("names in sentences", () => {
  it("splits a sentence around the names it mentions, longest first", () => {
    expect(
      nameParts("Not checked: your access to kind-weave-test was removed", [
        "kind-weave-test",
      ]),
    ).toEqual([
      { text: "Not checked: your access to ", name: false },
      { text: "kind-weave-test", name: true },
      { text: " was removed", name: false },
    ]);
    expect(
      nameParts("a-b and a-b-c", ["a-b", "a-b-c"]).map((part) => part.text),
    ).toEqual(["a-b", " and ", "a-b-c"]);
    expect(nameParts("No names here", [""])).toEqual([
      { text: "No names here", name: false },
    ]);
  });
});
