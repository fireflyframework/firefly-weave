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
// UTC calendars for schedules, checked by the same rules as the platform's
// validate_calendar: five fields, at most one of the two day fields set, and
// numbers, ranges, lists and steps only. Studio computes the next times a
// calendar fires as the platform's scheduler does (strictly after a moment,
// minute by minute), and offers presets people choose from.

/** One field's name and bounds, in the order a calendar lists them. */
const FIELDS = [
  { name: "minute", low: 0, high: 59 },
  { name: "hour", low: 0, high: 23 },
  { name: "day of month", low: 1, high: 31 },
  { name: "month", low: 1, high: 12 },
  { name: "day of week", low: 0, high: 6 },
] as const;
const TERM = /^(?:\*|[0-9]{1,2}(?:-[0-9]{1,2})?)(?:\/[0-9]{1,2})?$/;
const MAX_LENGTH = 256;
const MAX_TERMS = 32;
/** Days in each month of a leap year: a day is realizable when some chosen month has it. */
const LONGEST_MONTH = [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
/** How far ahead Studio looks for the next times, in days (enough for five 29 Februaries). */
const SEARCH_DAYS = 366 * 40;

export const WEEKDAYS = [
  "Sunday",
  "Monday",
  "Tuesday",
  "Wednesday",
  "Thursday",
  "Friday",
  "Saturday",
] as const;
/** The minute steps that repeat evenly through every hour. */
export const EVERY_MINUTES = [1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30] as const;

interface Calendar {
  minutes: number[];
  hours: number[];
  days: Set<number>;
  months: Set<number>;
  weekdays: Set<number>;
}

const fieldsOf = (expression: string) =>
  expression.trim() ? expression.trim().split(/\s+/) : [];

function parse(
  expression: string,
  timezone: string,
): { calendar: Calendar } | { problem: string } {
  if (timezone !== "UTC" || expression.length > MAX_LENGTH)
    return { problem: "Use a UTC calendar of at most 256 characters." };
  const fields = fieldsOf(expression);
  if (fields.length !== 5)
    return {
      problem:
        "Use five fields: minute, hour, day of month, month and day of week.",
    };
  if (fields[2] !== "*" && fields[4] !== "*")
    return {
      problem:
        "Set the day of month or the day of week, not both. Leave the other as *.",
    };
  const expanded: number[][] = [];
  for (const [index, value] of fields.entries()) {
    const { name, low, high } = FIELDS[index];
    const terms = value.split(",");
    if (terms.length > MAX_TERMS)
      return { problem: "Use at most 32 values in one field." };
    const selected = new Set<number>();
    for (const term of terms) {
      if (!TERM.test(term))
        return {
          problem:
            "Use numbers, ranges like 1-5, lists like 1,15 and steps like */15.",
        };
      const [base, step] = term.split("/");
      const stride = step === undefined ? 1 : Number(step);
      if (
        stride < 1 ||
        (step !== undefined && base !== "*" && !base.includes("-"))
      )
        return {
          problem:
            "Put a step of 1 or more after * or a range, like */15 or 0-30/5.",
        };
      const bounds = base === "*" ? [low, high] : base.split("-").map(Number);
      const first = bounds[0];
      const last = bounds[bounds.length - 1];
      if (!(low <= first && first <= last && last <= high))
        return {
          problem: `The ${name} must be between ${low} and ${high}${index === 4 ? " (Sunday is 0)" : ""}.`,
        };
      for (let value = first; value <= last; value += stride)
        selected.add(value);
    }
    expanded.push([...selected].sort((a, b) => a - b));
  }
  const [minutes, hours, days, months, weekdays] = expanded;
  if (
    !months.some((month) => days.some((day) => day <= LONGEST_MONTH[month - 1]))
  )
    return { problem: "None of the chosen months has that day." };
  return {
    calendar: {
      minutes,
      hours,
      days: new Set(days),
      months: new Set(months),
      weekdays: new Set(weekdays),
    },
  };
}

/** Why the platform would refuse this calendar, or null when it accepts it. */
export function validateCalendar(
  expression: string,
  timezone = "UTC",
): string | null {
  const parsed = parse(expression, timezone);
  return "problem" in parsed ? parsed.problem : null;
}

/** The next `count` times the calendar fires strictly after `from`; none for a calendar the platform refuses. */
export function nextOccurrences(
  expression: string,
  from: Date,
  count: number,
): Date[] {
  const parsed = parse(expression, "UTC");
  if ("problem" in parsed) return [];
  const { minutes, hours, days, months, weekdays } = parsed.calendar;
  const earliest = Math.floor(from.getTime() / 60000) * 60000 + 60000;
  const start = new Date(earliest);
  const found: Date[] = [];
  const day = new Date(
    Date.UTC(start.getUTCFullYear(), start.getUTCMonth(), start.getUTCDate()),
  );
  for (
    let searched = 0;
    searched < SEARCH_DAYS && found.length < count;
    searched++
  ) {
    if (!months.has(day.getUTCMonth() + 1)) {
      day.setUTCMonth(day.getUTCMonth() + 1, 1);
      continue;
    }
    // One of the two day fields is always every day, so a day fires when it is in both.
    if (days.has(day.getUTCDate()) && weekdays.has(day.getUTCDay()))
      for (const hour of hours)
        for (const minute of minutes) {
          const moment = Date.UTC(
            day.getUTCFullYear(),
            day.getUTCMonth(),
            day.getUTCDate(),
            hour,
            minute,
          );
          if (moment >= earliest && found.length < count)
            found.push(new Date(moment));
        }
    day.setUTCDate(day.getUTCDate() + 1);
  }
  return found;
}

export type SchedulePreset =
  | { kind: "minutes"; every: number }
  | { kind: "daily"; hour: number; minute: number }
  | { kind: "weekly"; day: number; hour: number; minute: number }
  | { kind: "monthly"; day: number; hour: number; minute: number }
  | { kind: "custom"; cron: string };

/** The calendar a preset stands for. */
export function presetCron(preset: SchedulePreset): string {
  switch (preset.kind) {
    case "minutes":
      return preset.every === 1 ? "* * * * *" : `*/${preset.every} * * * *`;
    case "daily":
      return `${preset.minute} ${preset.hour} * * *`;
    case "weekly":
      return `${preset.minute} ${preset.hour} * * ${preset.day}`;
    case "monthly":
      return `${preset.minute} ${preset.hour} ${preset.day} * *`;
    case "custom":
      return fieldsOf(preset.cron).join(" ");
  }
}

const single = (field: string) =>
  /^[0-9]{1,2}$/.test(field) ? Number(field) : undefined;

/** The preset that makes this calendar, or a custom one. */
export function readPreset(cron: string): SchedulePreset {
  const normalized = fieldsOf(cron).join(" ");
  const custom: SchedulePreset = { kind: "custom", cron: normalized };
  if (validateCalendar(normalized) !== null) return custom;
  const [minuteField, hourField, dayField, monthField, weekdayField] =
    normalized.split(" ");
  if (monthField !== "*") return custom;
  if (hourField === "*" && dayField === "*" && weekdayField === "*") {
    const every =
      minuteField === "*" ? 1 : /^\*\/([0-9]{1,2})$/.exec(minuteField)?.[1];
    const step = Number(every);
    return (EVERY_MINUTES as readonly number[]).includes(step)
      ? { kind: "minutes", every: step }
      : custom;
  }
  const minute = single(minuteField);
  const hour = single(hourField);
  if (minute === undefined || hour === undefined) return custom;
  if (dayField === "*" && weekdayField === "*")
    return { kind: "daily", hour, minute };
  const weekday = single(weekdayField);
  if (dayField === "*" && weekday !== undefined)
    return { kind: "weekly", day: weekday, hour, minute };
  const day = single(dayField);
  if (weekdayField === "*" && day !== undefined)
    return { kind: "monthly", day, hour, minute };
  return custom;
}

const clock = (hour: number, minute: number) =>
  `${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}`;

/** A short name for a calendar, as the activation dialog and the step details show it. */
export function describeCalendar(cron: string): string {
  const normalized = fieldsOf(cron).join(" ");
  const preset = readPreset(normalized);
  switch (preset.kind) {
    case "minutes":
      return preset.every === 1
        ? "Every minute"
        : `Every ${preset.every} minutes`;
    case "daily":
      return `Every day ${clock(preset.hour, preset.minute)} UTC`;
    case "weekly":
      return `${WEEKDAYS[preset.day]}s ${clock(preset.hour, preset.minute)} UTC`;
    case "monthly":
      return `Day ${preset.day} of each month ${clock(preset.hour, preset.minute)} UTC`;
    case "custom": {
      const [minuteField, hourField, dayField, monthField, weekdayField] =
        normalized.split(" ");
      const minute = single(minuteField);
      const hour = single(hourField);
      const days =
        weekdayField === "1-5"
          ? "Weekdays"
          : weekdayField === "0,6" || weekdayField === "6,0"
            ? "Weekends"
            : undefined;
      if (
        validateCalendar(normalized) === null &&
        days &&
        minute !== undefined &&
        hour !== undefined &&
        dayField === "*" &&
        monthField === "*"
      )
        return `${days} ${clock(hour, minute)} UTC`;
      return `${normalized} UTC`;
    }
  }
}
