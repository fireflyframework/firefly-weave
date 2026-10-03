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
import { describe, expect, it } from "vitest";
import {
  runStatus,
  statusLabel,
  statusOptions,
  statusTone,
  toneAttribute,
} from "../src/app/status-labels";

describe("status vocabulary", () => {
  it("names run states in sentence case with their tones", () => {
    expect(statusLabel("run", "failed")).toBe("Failed");
    expect(statusTone("run", "failed")).toBe("danger");
    expect(statusLabel("run", "running")).toBe("Running");
    expect(statusTone("run", "running")).toBe("info");
    expect(statusLabel("run", "suspended")).toBe("On hold");
    expect(statusLabel("run", "cancelled")).toBe("Canceled");
    expect(statusLabel("run", "timed_out")).toBe("Timed out");
    expect(statusTone("run", "succeeded")).toBe("success");
    expect(statusTone("run", "waiting")).toBe("warning");
  });

  it("uses one name per task state and says whose claim it is", () => {
    expect(statusLabel("task", "ready")).toBe("Ready to claim");
    expect(statusTone("task", "ready")).toBe("warning");
    expect(statusLabel("task", "claimed")).toBe("Claimed");
    expect(statusLabel("task", "claimed", { mine: true })).toBe(
      "Claimed by you",
    );
    expect(statusTone("task", "claimed")).toBe("info");
    expect(statusLabel("task", "cancelled")).toBe("Canceled");
  });

  it("explains email, connection, worker and account values", () => {
    expect(statusLabel("email", "queued")).toBe("Waiting to send");
    expect(statusLabel("email", "accepted")).toBe("Sent to mail server");
    expect(statusLabel("email", "unknown")).toBe("Send status unknown");
    expect(statusTone("connection", "unavailable")).toBe("danger");
    expect(statusLabel("worker", "revoked")).toBe("Revoked");
    expect(statusTone("worker", "revoked")).toBe("danger");
    expect(statusLabel("account", "human")).toBe("Person");
    expect(statusLabel("account", "application")).toBe("App");
  });

  it("never shows raw or capitalized-every-word values", () => {
    expect(statusLabel("run", "new_state")).toBe("New state");
    expect(statusLabel("run", undefined)).toBe("Status unavailable");
    expect(statusTone("run", "new_state")).toBe("neutral");
    const all = (["run", "task", "email"] as const).flatMap((kind) =>
      statusOptions(kind).map(([, label]) => label),
    );
    expect(all).not.toContain("Ready To Claim");
    expect(all.every((label) => /^[A-Z][a-z ]*$/.test(label))).toBe(true);
  });

  it("leaves neutral pills without a tone attribute", () => {
    expect(toneAttribute("neutral")).toBeNull();
    expect(toneAttribute("danger")).toBe("danger");
  });

  it("reads a run's status, including operator holds", () => {
    expect(runStatus({ state: { status: "waiting" } })).toBe("waiting");
    expect(
      runStatus({ state: { status: "waiting", manual_paused: true } }),
    ).toBe("paused");
    expect(runStatus({ unavailable: true })).toBe("unavailable");
    expect(statusLabel("run", "paused")).toBe("Paused");
  });
});
