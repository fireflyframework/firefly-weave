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
// Plain-language copy for simulation. Debugger codes are value-free and the
// same for local and remote platforms (operations/debug/models.py), so each
// one maps to a sentence that says what to do next. The code itself is shown
// separately as a support code.

/** Simulation failures by stable code. */
export const debugMessages: Readonly<Record<string, string>> = {
  "WV-DEBUG-MOCK-KEY":
    "An action result is attached to a step that isn't an action in this workflow. Start the simulation again from the setup.",
  "WV-DEBUG-MOCK-OUTPUT":
    "An action result doesn't match what that action returns. Check the action results and start again.",
  "WV-DEBUG-MISSING_MOCK":
    "The simulation reached an action that has no result to return. Start over and fill in that action's result.",
  "WV-DEBUG-HUMAN-TASK":
    "That human task isn't waiting for a decision right now. Continue the simulation until it is.",
  "WV-DEBUG-SIGNAL":
    "No step in this workflow waits for a signal with that name.",
  "WV-DEBUG-STATE":
    "The simulated run has already finished, so it can't accept more input. Start over to try again.",
  "WV-DEBUG-TIME": "Enter a whole number of seconds, zero or more.",
  "WV-DEBUG-LIMIT":
    "The simulation reached its size, step or time limit. Start over with a smaller input, or advance time in smaller amounts.",
  "WV-DEBUG-BREAKPOINT":
    "A breakpoint points at a step that isn't in this workflow. Start the simulation again.",
  "WV-DEBUG-COMMAND":
    "Studio sent a simulation command the platform doesn't understand. Reload Studio and try again.",
  "WV-DEBUG-REVISION":
    "This simulation changed in another window. Close it and start a new one.",
  "WV-DEBUG-EXPIRED":
    "This simulation expired. Start a new one to keep testing.",
  "WV-DEBUG-REQUEST":
    "The platform couldn't read this simulation request. Start a new simulation.",
  "WV-VALIDATION":
    "Some values don't match the workflow's schemas. Check the input, the action results and any signal or decision data.",
  "WV-FORBIDDEN":
    "Your account can't run simulations in this workspace. Ask an administrator for the simulate permission.",
  "WV-DENIED":
    "Your account can't run simulations in this workspace. Ask an administrator for the simulate permission.",
};

/** Explains a simulation failure; unknown codes keep the caller's message. */
export function simulationMessage(code: string, fallback: string): string {
  return debugMessages[code] ?? fallback;
}

/** Status of the simulated run as shown in the panel header. */
export const statusCopy: Readonly<
  Record<string, { label: string; detail: string }>
> = {
  queued: {
    label: "Ready",
    detail: "Use Step or Continue to start the simulated run.",
  },
  running: {
    label: "Running",
    detail: "The simulated run is moving through its steps.",
  },
  waiting: {
    label: "Waiting",
    detail: "The run waits for time to pass, a signal or a decision.",
  },
  suspended: {
    label: "Suspended",
    detail: "The simulated run is suspended.",
  },
  paused: {
    label: "Paused at a breakpoint",
    detail: "Use Step or Continue to go on.",
  },
  succeeded: {
    label: "Finished",
    detail: "The simulated run completed successfully.",
  },
  failed: {
    label: "Failed",
    detail: "The simulated run stopped with a problem.",
  },
  cancelled: { label: "Canceled", detail: "The simulated run was canceled." },
  timed_out: {
    label: "Timed out",
    detail: "A time limit passed before the run could finish.",
  },
};

export function statusLabel(status: string) {
  return statusCopy[status]?.label ?? status.replace(/_/g, " ");
}
export function statusDetail(status: string) {
  return statusCopy[status]?.detail ?? "";
}

export interface SimulationEvent {
  type: string;
  data?: Record<string, unknown>;
  timestamp?: string;
}

/** One line of the activity list for a runtime event. */
export function eventText(event: SimulationEvent): string {
  const data = event.data ?? {};
  const node = typeof data["node_id"] === "string" ? data["node_id"] : "";
  switch (event.type) {
    case "started":
      return "Run started";
    case "task_completed":
      return `Action ${node} returned its result`;
    case "signal_received":
      return `Step ${node} received its signal`;
    case "human_completed": {
      const output = data["output"];
      const decision =
        output && typeof output === "object" && "decision" in output
          ? String((output as Record<string, unknown>)["decision"])
          : "";
      return decision
        ? `Human task ${node} decided: ${decision}`
        : `Human task ${node} completed`;
    }
    case "wait_elapsed":
      return `Wait ${node} finished`;
    case "timed_out":
      return node === "@run" || !node
        ? "The run reached its time limit"
        : `Step ${node} reached its time limit`;
    default:
      return event.type.replace(/_/g, " ");
  }
}

const units: [number, string, string][] = [
  [86400, "day", "days"],
  [3600, "h", "h"],
  [60, "min", "min"],
  [1, "s", "s"],
];

/** Short duration such as "1 day 2 h" or "90 s"; at most two parts. */
export function durationText(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "0 s";
  const parts: string[] = [];
  let rest = Math.round(seconds);
  for (const [size, one, many] of units) {
    if (rest >= size && parts.length < 2) {
      const count = Math.floor(rest / size);
      parts.push(`${count} ${count === 1 ? one : many}`);
      rest -= count * size;
    }
  }
  return parts.join(" ");
}
