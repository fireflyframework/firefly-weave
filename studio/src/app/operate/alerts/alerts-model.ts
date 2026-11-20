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
// What the Alerts page says: each signal in words, thresholds as percents
// for ratios (sent as 0.1, shown as 10 %), a rule's condition and where it
// stands now, a firing alert's value against its threshold, each channel's
// notification, how a resolved alert ended, and the rule a form makes. Pure:
// the page and the tests call these.
import { relativeTime, shortId, toDate } from "../../format";
import {
  alertSignals,
  type Alert,
  type AlertNotification,
  type AlertPreview,
  type AlertRuleDraft,
  type AlertSignal,
  type AlertUnit,
  type Comparator,
  type RuleState,
  type Severity,
  type ResolvedReason,
} from "./alerts-api";

export type SelectorKey = "workflow" | "task_type" | "target_id";
export interface SignalWords {
  name: string;
  /** What the value counts, for the editor's hint. */
  hint: string;
  unit: AlertUnit;
  /** A window signal reads over a window; a gauge reads what is now. */
  window: boolean;
  /** Compares only once enough runs ended in the window. */
  sampled: boolean;
  /** The one series a rule may narrow it to, if any. */
  selector: SelectorKey | null;
}
const gauge = (
  name: string,
  hint: string,
  unit: AlertUnit = "count",
  selector: SelectorKey | null = null,
): SignalWords => ({
  name,
  hint,
  unit,
  window: false,
  sampled: false,
  selector,
});

/** Every signal this Studio knows, in the platform's order. */
export const SIGNAL_WORDS: Record<AlertSignal, SignalWords> = {
  "run.failure_rate": {
    name: "Run failure rate",
    hint: "Percent of ended runs that failed or timed out.",
    unit: "ratio",
    window: true,
    sampled: true,
    selector: "workflow",
  },
  "run.failures": {
    name: "Failed runs",
    hint: "Runs that failed or timed out in the window.",
    unit: "count",
    window: true,
    sampled: false,
    selector: "workflow",
  },
  "run.duration_p95": {
    name: "Run duration, 95th percentile",
    hint: "How long the slowest of 95 in 100 ended runs took.",
    unit: "seconds",
    window: true,
    sampled: true,
    selector: "workflow",
  },
  "queue.oldest_ready_seconds": gauge(
    "Oldest ready task",
    "How long the oldest task ready for a worker has waited.",
    "seconds",
    "task_type",
  ),
  "queue.ready_tasks": gauge(
    "Ready tasks",
    "Tasks ready and waiting for a worker.",
    "count",
    "task_type",
  ),
  "outbox.pending": gauge(
    "Event deliveries waiting",
    "Integration events not yet handed to their subscribers.",
  ),
  "provider.pending": gauge(
    "Provider messages waiting",
    "Messages from providers not yet taken in.",
  ),
  "incidents.active": gauge("Open incidents", "Incidents not yet resolved."),
  "workers.online": gauge(
    "Workers online",
    "Workers in touch with the platform.",
  ),
  "workers.free_slots": gauge(
    "Free worker slots",
    "Task slots free on the workers online.",
  ),
  "targets.runner_offline": gauge(
    "Runners offline",
    "Deployment targets whose runner isn't in touch.",
    "count",
    "target_id",
  ),
  "deployments.reconciliation_required": gauge(
    "Deployments to reconcile",
    "Deployments whose target no longer matches what was deployed.",
    "count",
    "target_id",
  ),
  "health.degraded": gauge(
    "Platform components degraded",
    "Platform components that are degraded or down, apart from the alert checker.",
  ),
};
const isKnown = (signal: string): signal is AlertSignal =>
  (alertSignals as readonly string[]).includes(signal);
/** A known signal's words, or null for a newer server's signal. */
export const signalWords = (signal: string): SignalWords | null =>
  isKnown(signal) ? SIGNAL_WORDS[signal] : null;
/** "Run failure rate"; a signal this Studio doesn't know keeps its name. */
export const signalName = (signal: string) =>
  signalWords(signal)?.name ?? signal;

// ------------------------------------------------------------------ numbers

const twoDecimals = (value: number) => Math.round(value * 100) / 100;
const plain = (value: number) =>
  twoDecimals(value).toLocaleString("en-US", { maximumFractionDigits: 2 });

/** 0.1 → "10 %"; two decimals at most. */
export function toPercent(ratio: number): string {
  return `${plain(ratio * 100)} %`;
}
/** The percent a field holds without its sign: 0.1 → "10". */
export const percentField = (ratio: number) => String(twoDecimals(ratio * 100));
/** "10" or "10 %" → 0.1; null for anything but a percent from 0 to 100. */
export function fromPercent(text: string): number | null {
  const value = text.trim().replace(/\s*%$/, "");
  if (!/^\d+(\.\d+)?$/.test(value)) return null;
  const percent = twoDecimals(Number(value));
  if (percent > 100) return null;
  return Number((percent / 100).toFixed(4));
}
/** A reading in its unit: "14.6 %", "42 s", "2"; "—" without one. */
export function valueText(
  value: number | null,
  unit: AlertUnit | null,
): string {
  if (value === null) return "—";
  if (unit === "ratio") return toPercent(value);
  if (unit === "seconds") return `${plain(value)} s`;
  return plain(value);
}

const comparatorWords: Record<Comparator, [string, string, string]> = {
  gt: ["Above", "above", ">"],
  gte: ["At or above", "at or above", "≥"],
  lt: ["Below", "below", "<"],
  lte: ["At or below", "at or below", "≤"],
};
/** The comparator menu: value and words. */
export const comparatorChoices = (
  Object.entries(comparatorWords) as [Comparator, [string, string, string]][]
).map(([value, [label]]) => ({ value, label }));

/** "15 min", "1 hour": a window or a For time, in seconds. */
function periodText(seconds: number): string {
  if (seconds === 3600) return "1 hour";
  if (seconds < 60) return `${seconds} s`;
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  if (minutes < 60) return rest ? `${minutes} min ${rest} s` : `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  return minutes % 60 ? `${hours} h ${minutes % 60} min` : `${hours} h`;
}
/** "15 minutes", "hour": a window inside "in the last …". */
function windowPhrase(seconds: number): string {
  if (seconds === 60) return "minute";
  if (seconds === 3600) return "hour";
  return `${Math.round(seconds / 60)} minutes`;
}

/** The unit a rule's threshold is in: its signal's, else what the server said. */
export const ruleUnit = (rule: {
  signal: string;
  state?: RuleState;
}): AlertUnit => signalWords(rule.signal)?.unit ?? rule.state?.unit ?? "count";

/** "Above 10 % over 15 min · at least 10 runs", "Below 1 · for 2 min". */
export function conditionText(
  rule: AlertRuleDraft & { state?: RuleState },
): string {
  const words = signalWords(rule.signal);
  let text = `${comparatorWords[rule.comparator][0]} ${valueText(rule.threshold, ruleUnit(rule))}`;
  if (rule.window_seconds) text += ` over ${periodText(rule.window_seconds)}`;
  if (rule.for_seconds > 0) text += ` · for ${periodText(rule.for_seconds)}`;
  if (words?.sampled && rule.min_samples > 1)
    text += ` · at least ${rule.min_samples.toLocaleString("en-US")} runs`;
  return text;
}

/** "45 s", "12 min", "2 h 10 min", "3 days": how long something lasted. */
export function spanText(milliseconds: number): string {
  const seconds = Math.max(0, Math.floor(milliseconds / 1000));
  if (seconds < 60) return `${seconds} s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48)
    return minutes % 60 ? `${hours} h ${minutes % 60} min` : `${hours} h`;
  const days = Math.floor(hours / 24);
  return `${days} days`;
}

const dayKey = (date: Date, timeZone?: string) =>
  new Intl.DateTimeFormat("en-CA", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    ...(timeZone ? { timeZone } : {}),
  }).format(date);
/** "Today 10:42", "Yesterday 22:40", "Tue 3 Nov 09:05", in the viewer's zone. */
export function startedText(
  value: string,
  now: Date = new Date(),
  timeZone?: string,
): string {
  const date = toDate(value);
  if (!date) return "";
  const time = new Intl.DateTimeFormat("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
    ...(timeZone ? { timeZone } : {}),
  }).format(date);
  const day = dayKey(date, timeZone);
  if (day === dayKey(now, timeZone)) return `Today ${time}`;
  if (day === dayKey(new Date(now.getTime() - 86_400_000), timeZone))
    return `Yesterday ${time}`;
  const prefix = new Intl.DateTimeFormat("en-GB", {
    weekday: "short",
    day: "numeric",
    month: "short",
    ...(timeZone ? { timeZone } : {}),
  })
    .format(date)
    .replace(",", "");
  return `${prefix} ${time}`;
}

// ------------------------------------------------------------------ severity, state

export type Tone = "success" | "warning" | "danger" | "info" | "subtle";
export const severityWords: Record<
  Severity,
  { label: string; icon: string; tone: Tone }
> = {
  critical: { label: "Critical", icon: "critical", tone: "danger" },
  warning: { label: "Warning", icon: "warning", tone: "warning" },
  info: { label: "Info", icon: "info", tone: "info" },
};

export interface StateLine {
  text: string;
  tone: Tone;
  icon: string;
}
/**
 * A rule's "Now": OK with its value, firing for how long, holding before it
 * fires, the last check failing, or why it isn't checked at all.
 */
export function stateLine(
  rule: AlertRuleDraft & { state: RuleState },
  context: {
    now: Date;
    /** The firing alert the rule points at, when it is shown. */
    alert?: Alert | null;
    /** A target's name by ID; "" when it can't be read. */
    target: (id: string) => string;
  },
): StateLine {
  const state = rule.state;
  const unit = state.unit ?? ruleUnit(rule);
  const value =
    state.value === null ? "" : ` · ${valueText(state.value, unit)}`;
  const since = spanText(context.now.getTime() - Date.parse(state.since));
  if (state.state === "unsupported")
    return {
      text: "Not checked: this server doesn't know the signal",
      tone: "subtle",
      icon: "off",
    };
  if (state.state === "unauthorized") {
    const id = rule.selector.target_id;
    const name = id ? context.target(id) || `target ${shortId(id)}` : "";
    return {
      text: name
        ? `Not checked: your access to ${name} was removed`
        : "Not checked: the access this rule needs was removed",
      tone: "warning",
      icon: "lock",
    };
  }
  if (state.error)
    return {
      text: `Last check failed ${relativeTime(state.error.at, context.now)}`,
      tone: "warning",
      icon: "warning",
    };
  if (state.state === "firing")
    return {
      text: `Firing for ${since}${value}${context.alert?.acknowledged_at ? " · acknowledged" : ""}`,
      tone: "danger",
      icon: "alertFiring",
    };
  if (state.state === "pending")
    return {
      text: `Condition met for ${since}${value}`,
      tone: "warning",
      icon: "stale",
    };
  if (state.no_data) return { text: "No data", tone: "subtle", icon: "info" };
  return { text: `OK${value}`, tone: "success", icon: "ok" };
}

// ------------------------------------------------------------------ alerts

/** "Firing · 14.6 % > 10 %": the latest value against the threshold. */
export function alertLine(alert: Alert): string {
  const value = alert.last_value ?? alert.value_at_start;
  return `Firing · ${valueText(value, alert.unit)} ${comparatorWords[alert.comparator][2]} ${valueText(alert.threshold, alert.unit)}`;
}

const failures: Record<string, string> = {
  mail_refused: "the mail server refused the message",
  mail_unknown: "the mail server didn't say whether it took the message",
  channel_deleted: "the channel was deleted",
  owner_unauthorized: "the channel's owner can't send mail anymore",
  recipient_not_member: "a recipient isn't a project member anymore",
  recipient_without_address: "a member hasn't set an alert address",
  recipient_not_allowed: "an outside address's domain isn't allowed",
  recipient_without_consent: "an outside address has no opt-in",
  approval_ended: "the change request no longer waits for approval",
};
/** "Sent", "Failed: the mail server refused the message", "Pending"… */
export function notificationText(notification: AlertNotification): string {
  const code = notification.code ?? "";
  switch (notification.state) {
    case "sent":
      return code === "mail_refused"
        ? "Sent to some recipients; the mail server refused the others"
        : "Sent";
    case "skipped_limit":
      return "Skipped: hourly limit reached";
    case "pending":
      return code === "mail_refused"
        ? "Pending: the mail server refused the message, trying again"
        : "Pending";
    default:
      return Object.hasOwn(failures, code)
        ? `Failed: ${failures[code]}`
        : "Failed";
  }
}
/** The icon and tone of a notification's line. */
export function notificationTone(notification: AlertNotification): StateLine {
  const text = notificationText(notification);
  if (notification.state === "sent")
    return { text, tone: "success", icon: "ok" };
  if (notification.state === "failed")
    return { text, tone: "danger", icon: "failCircle" };
  return { text, tone: "warning", icon: "stale" };
}

const endings: Record<ResolvedReason, string> = {
  threshold: "Back within the threshold",
  no_data: "No data to check",
  rule_changed: "Rule changed",
  rule_disabled: "Rule disabled",
  rule_deleted: "Rule deleted",
};
/** How a resolved alert ended. */
export const resolvedText = (reason: ResolvedReason | null) =>
  reason ? endings[reason] : "Resolved";

// ------------------------------------------------------------------ preview

/** What a preview says: the value, whether the condition holds, or why there's no data. */
export function previewWords(
  preview: AlertPreview,
  rule: AlertRuleDraft,
): { value: string; status: string; detail: string } {
  if (preview.value === null) {
    const status =
      preview.no_data_reason === "too_few_samples" && rule.window_seconds
        ? `No data: fewer than ${rule.min_samples.toLocaleString("en-US")} runs ended in the last ${windowPhrase(rule.window_seconds)}.`
        : "No data: there is nothing to read for this signal yet.";
    return {
      value: "—",
      status,
      detail:
        "With no data, the rule stays as it is: it doesn't fire and doesn't resolve.",
    };
  }
  const value = valueText(preview.value, preview.unit);
  const threshold = valueText(rule.threshold, preview.unit);
  const words = comparatorWords[rule.comparator][1];
  return {
    value,
    status: `Current value ${value} (evaluated now)`,
    detail: preview.condition_met
      ? `The condition is met: ${value} is ${words} ${threshold}. Saving fires the rule once the condition has held for its For time.`
      : `The condition isn't met: ${value} isn't ${words} ${threshold}. Saving doesn't fire anything now.`,
  };
}

// ------------------------------------------------------------------ the rule form

export interface RuleForm {
  name: string;
  enabled: boolean;
  signal: string;
  /** The workflow, task type or target ID the signal allows; "" for all. */
  selector: string;
  comparator: Comparator;
  /** A percent for ratio signals, else a number in the signal's unit. */
  threshold: string;
  window: string;
  forSeconds: string;
  minSamples: string;
  severity: Severity;
  channels: string[];
  renotify: string;
}
export const emptyRuleForm = (): RuleForm => ({
  name: "",
  enabled: true,
  signal: "run.failure_rate",
  selector: "",
  comparator: "gt",
  threshold: "",
  window: "900",
  forSeconds: "0",
  minSamples: "1",
  severity: "warning",
  channels: [],
  renotify: "0",
});
/** The form showing a rule or a preset's rule as it is. */
export function formFromRule(rule: AlertRuleDraft): RuleForm {
  const words = signalWords(rule.signal);
  const key = words?.selector;
  return {
    name: rule.name,
    enabled: rule.enabled,
    signal: rule.signal,
    selector: (key && rule.selector[key]) || "",
    comparator: rule.comparator,
    threshold:
      words?.unit === "ratio"
        ? percentField(rule.threshold)
        : String(rule.threshold),
    window: String(rule.window_seconds ?? 900),
    forSeconds: String(rule.for_seconds),
    minSamples: String(rule.min_samples),
    severity: rule.severity,
    channels: [...rule.channel_ids],
    renotify: String(rule.renotify_seconds),
  };
}

export const windowChoices = [
  { value: "60", label: "over the last minute" },
  { value: "300", label: "over the last 5 minutes" },
  { value: "900", label: "over the last 15 minutes" },
  { value: "3600", label: "over the last hour" },
];
const forSteps = [0, 30, 60, 120, 300, 600, 900, 1800, 3600];
/** The For menu, with a saved value that isn't one of its steps. */
export function forChoices(current: string) {
  const steps = forSteps.includes(Number(current))
    ? forSteps
    : [...forSteps, Number(current)].sort((a, b) => a - b);
  return steps.map((seconds) => ({
    value: String(seconds),
    label:
      seconds === 0
        ? "At once"
        : seconds === 3600
          ? "1 hour"
          : seconds < 60
            ? `${seconds} seconds`
            : seconds % 60
              ? periodText(seconds)
              : `${seconds / 60} minute${seconds === 60 ? "" : "s"}`,
  }));
}
const renotifySteps = [0, 900, 1800, 3600, 14_400, 86_400];
/** The Repeat while firing menu, with a saved value that isn't one of its steps. */
export function renotifyChoices(current: string) {
  const steps = renotifySteps.includes(Number(current))
    ? renotifySteps
    : [...renotifySteps, Number(current)].sort((a, b) => a - b);
  const names: Record<number, string> = {
    0: "Never",
    900: "Every 15 minutes",
    1800: "Every 30 minutes",
    3600: "Every hour",
    14_400: "Every 4 hours",
    86_400: "Every day",
  };
  return steps.map((seconds) => ({
    value: String(seconds),
    label: names[seconds] ?? `Every ${periodText(seconds)}`,
  }));
}

const NAME_LIMIT = 120;
const CHANNEL_LIMIT = 5;
const workflowName = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,199}$/;
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
// Names reach mail subjects: visible text only.
const hidden = /[\p{Cc}\p{Cf}\u2028\u2029]/u;
export type RuleProblems = Partial<Record<keyof RuleForm, string>>;

/** The rule the form describes, or what each field needs. */
export function ruleDraft(
  form: RuleForm,
): { draft: AlertRuleDraft } | { problems: RuleProblems } {
  const problems: RuleProblems = {};
  const words = signalWords(form.signal);
  const name = form.name.trim();
  if (!name) problems.name = "Name the rule.";
  else if (name.length > NAME_LIMIT)
    problems.name = `Keep the name to ${NAME_LIMIT} characters or fewer.`;
  else if (hidden.test(name))
    problems.name = "Use visible characters only: no tabs or line breaks.";
  const unit = words?.unit ?? "count";
  let threshold: number | null = null;
  if (unit === "ratio") {
    threshold = fromPercent(form.threshold);
    if (threshold === null)
      problems.threshold = "Enter a percent from 0 to 100.";
  } else {
    const text = form.threshold.trim();
    const value = Number(text);
    if (/^\d+(\.\d+)?$/.test(text) && value <= 1e9) threshold = value;
    else problems.threshold = "Enter a number from 0 to 1,000,000,000.";
  }
  const sampled = !!words?.sampled;
  const minimum = Number(form.minSamples.trim());
  if (
    sampled &&
    !(/^\d+$/.test(form.minSamples.trim()) && minimum >= 1 && minimum <= 10_000)
  )
    problems.minSamples = "Enter a whole number from 1 to 10,000.";
  const key = words?.selector ?? null;
  const narrowed = form.selector.trim();
  if (key === "workflow" && narrowed && !workflowName.test(narrowed))
    problems.selector =
      "Use letters, digits, dots, dashes and underscores, starting with a letter or digit.";
  if (key === "target_id" && narrowed && !uuid.test(narrowed))
    problems.selector = "Choose a target from the list.";
  if (
    key === "task_type" &&
    narrowed &&
    (narrowed.length > 200 || hidden.test(narrowed))
  )
    problems.selector = "Use up to 200 visible characters.";
  if (form.channels.length > CHANNEL_LIMIT)
    problems.channels = `Choose up to ${CHANNEL_LIMIT} channels.`;
  if (Object.keys(problems).length || threshold === null) return { problems };
  return {
    draft: {
      name,
      enabled: form.enabled,
      signal: form.signal,
      selector: key && narrowed ? { [key]: narrowed } : {},
      comparator: form.comparator,
      threshold,
      window_seconds: words?.window ? Number(form.window) : null,
      for_seconds: Number(form.forSeconds),
      min_samples: sampled ? minimum : 1,
      severity: form.severity,
      channel_ids: [...form.channels],
      renotify_seconds: Number(form.renotify),
    },
  };
}

// ------------------------------------------------------------------ recipients

/** Why the platform refused an outside address (WV-ALERT-RECIPIENT reasons). */
export function recipientProblem(
  reason: string,
  address: string,
  environment: string,
): string {
  switch (reason) {
    case "domain_not_allowed":
      return `${address.slice(address.lastIndexOf("@") + 1)} isn't an allowed email domain in ${environment}.`;
    case "no_consent":
      return `${address} has no opt-in in ${environment}. An administrator can record one in Settings › Administration.`;
    case "consent_by_creator":
      return "You recorded this opt-in, so another administrator adds the address.";
    case "not_member":
      return "This person isn't a member of the project anymore.";
    default:
      return "The platform refused this recipient.";
  }
}

/**
 * A sentence split around the names it mentions, so a page can keep each
 * name whole on its line ("kind-weave-test" never breaks at its hyphen).
 */
export function nameParts(
  text: string,
  names: readonly string[],
): { text: string; name: boolean }[] {
  const found = names.filter(Boolean).sort((a, b) => b.length - a.length);
  const parts: { text: string; name: boolean }[] = [];
  let rest = text;
  while (rest) {
    let at = -1;
    let name = "";
    for (const candidate of found) {
      const index = rest.indexOf(candidate);
      if (index !== -1 && (at === -1 || index < at)) {
        at = index;
        name = candidate;
      }
    }
    if (at === -1) {
      parts.push({ text: rest, name: false });
      break;
    }
    if (at > 0) parts.push({ text: rest.slice(0, at), name: false });
    parts.push({ text: name, name: true });
    rest = rest.slice(at + name.length);
  }
  return parts;
}
