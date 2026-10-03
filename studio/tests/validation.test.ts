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
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ValidationContext,
  ValidationResult,
  ValidationService,
  countDiagnostics,
  countLine,
  selectMode,
  summarize,
} from "../src/app/designer/validation";

const passed = (extra: Partial<ValidationResult> = {}): ValidationResult => ({
  validationOk: true,
  errorCount: 0,
  diagnostics: [],
  partial: true,
  ...extra,
});
const compiled = (source: string): ValidationResult => ({
  validationOk: true,
  errorCount: 0,
  diagnostics: [],
  partial: false,
  ok: true,
  artifact: { digest: `sha256:${source.length}` },
});
const failed: ValidationResult = {
  validationOk: false,
  errorCount: 1,
  diagnostics: [
    {
      code: "WV-COMP-UNAVAILABLE_REFERENCE",
      severity: "error",
      message: "Referenced step does not dominate this expression.",
      path: "/spec/steps/1/value/ref",
    },
    { code: "WV-COMP-REFERENCE_PRESENCE", severity: "warning", message: "x" },
  ],
  partial: true,
};

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (error: unknown) => void;
}
function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void, reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function harness(
  context: Partial<ValidationContext> = {},
  options: { delay?: number; cacheSize?: number } = {},
) {
  const calls: { kind: "local" | "compile"; source: string; at: number }[] = [];
  const local = vi.fn(async (source: string) => {
    calls.push({ kind: "local", source, at: Date.now() });
    return passed();
  });
  const compile = vi.fn(async (source: string) => {
    calls.push({ kind: "compile", source, at: Date.now() });
    return compiled(source);
  });
  const ctx: ValidationContext = {
    connected: false,
    canCompile: false,
    scope: "",
    ...context,
  };
  const changes = vi.fn();
  const service = new ValidationService({
    transport: { local, compile },
    context: () => ctx,
    onChange: changes,
    ...options,
  });
  return { service, local, compile, calls, ctx, changes };
}

beforeEach(() => {
  vi.useFakeTimers();
});
afterEach(() => {
  vi.useRealTimers();
});

describe("debounced local validation", () => {
  it("runs once, 700 ms after the last change, with the latest source", async () => {
    const { service, local } = harness();
    for (const [i, text] of ["a", "ab", "abc", "abcd"].entries()) {
      expect(service.schedule(text, "yaml")).toBe(true);
      await vi.advanceTimersByTimeAsync(i === 3 ? 0 : 150);
    }
    await vi.advanceTimersByTimeAsync(699);
    expect(local).not.toHaveBeenCalled();
    expect(service.snapshot.status).toBe("scheduled");
    await vi.advanceTimersByTimeAsync(1);
    expect(local).toHaveBeenCalledTimes(1);
    expect(local).toHaveBeenCalledWith("abcd", "yaml");
    expect(service.snapshot).toMatchObject({
      status: "done",
      mode: "local",
      source: "abcd",
      stale: false,
    });
  });
  it("keeps the delay within 600–800 ms", async () => {
    for (const [requested, expected] of [
      [100, 600],
      [650, 650],
      [5000, 800],
    ]) {
      const { service, local } = harness({}, { delay: requested });
      service.schedule("x", "yaml");
      await vi.advanceTimersByTimeAsync(expected - 1);
      expect(local).not.toHaveBeenCalled();
      await vi.advanceTimersByTimeAsync(1);
      expect(local).toHaveBeenCalledTimes(1);
    }
  });
  it("never sends more than one request per 600 ms while typing", async () => {
    const { service, calls } = harness({}, { delay: 600 });
    // Bursts of keystrokes 120 ms apart with irregular pauses.
    let text = "";
    for (let burst = 0; burst < 12; burst++) {
      for (let key = 0; key < 1 + (burst % 4); key++) {
        text += "x";
        service.schedule(text, "yaml");
        await vi.advanceTimersByTimeAsync(120);
      }
      await vi.advanceTimersByTimeAsync(250 + ((burst * 137) % 600));
    }
    await vi.advanceTimersByTimeAsync(1000);
    expect(calls.length).toBeGreaterThan(1);
    for (let i = 1; i < calls.length; i++)
      expect(calls[i].at - calls[i - 1].at).toBeGreaterThanOrEqual(600);
  });
  it("skips unchanged source (layout-only edits) and keeps the diagnostics", async () => {
    const { service, local } = harness();
    service.schedule("same", "yaml");
    await vi.advanceTimersByTimeAsync(700);
    const before = service.snapshot.result;
    expect(service.schedule("same", "yaml")).toBe(false);
    await vi.advanceTimersByTimeAsync(2000);
    expect(local).toHaveBeenCalledTimes(1);
    expect(service.snapshot.result).toBe(before);
    expect(service.schedule("same", "json")).toBe(true);
  });
  it("applies only the newest answer when responses arrive out of order", async () => {
    const { service, local } = harness();
    const first = deferred<ValidationResult>();
    const second = deferred<ValidationResult>();
    local
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise);
    service.schedule("v1", "yaml");
    await vi.advanceTimersByTimeAsync(700);
    service.schedule("v2", "yaml");
    expect(service.snapshot.stale).toBe(false);
    await vi.advanceTimersByTimeAsync(700);
    second.resolve(failed);
    await vi.advanceTimersByTimeAsync(0);
    first.resolve(passed());
    await vi.advanceTimersByTimeAsync(0);
    expect(service.snapshot).toMatchObject({
      source: "v2",
      result: failed,
      status: "done",
    });
  });
  it("marks an older result stale while a newer source waits", async () => {
    const { service } = harness();
    service.schedule("v1", "yaml");
    await vi.advanceTimersByTimeAsync(700);
    service.schedule("v2", "yaml");
    expect(service.snapshot).toMatchObject({
      source: "v1",
      stale: true,
      status: "scheduled",
    });
    expect(service.hasResultFor("v1", "yaml")).toBe(true);
    expect(service.hasResultFor("v2", "yaml")).toBe(false);
  });
  it("records failures without throwing and keeps the last result", async () => {
    const { service, local } = harness();
    service.schedule("ok", "yaml");
    await vi.advanceTimersByTimeAsync(700);
    const error = new Error("host unavailable");
    local.mockRejectedValueOnce(error);
    service.schedule("broken", "yaml");
    await vi.advanceTimersByTimeAsync(700);
    expect(service.snapshot).toMatchObject({
      status: "failed",
      error,
      source: "ok",
    });
    expect(service.snapshot.result).not.toBeNull();
    // A failed check is retried on the next change, even a layout-only one.
    local.mockResolvedValueOnce(passed());
    expect(service.schedule("broken", "yaml")).toBe(true);
    await vi.advanceTimersByTimeAsync(700);
    expect(service.snapshot).toMatchObject({
      status: "done",
      source: "broken",
    });
  });
  it("notifies on every change and stops after dispose", async () => {
    const { service, local, changes } = harness();
    service.schedule("a", "yaml");
    await vi.advanceTimersByTimeAsync(700);
    expect(changes.mock.calls.map(([s]) => s.status)).toEqual([
      "scheduled",
      "running",
      "done",
    ]);
    service.schedule("b", "yaml");
    service.dispose();
    await vi.advanceTimersByTimeAsync(2000);
    expect(local).toHaveBeenCalledTimes(1);
  });
});

describe("manual validation and the project compile", () => {
  const project = {
    connected: true,
    canCompile: true,
    scope: "/studio/api/api/v1/tenants/t/projects/p",
  };
  it("selects the project compile only when connected with compile access", () => {
    expect(selectMode({ connected: false, canCompile: true, scope: "" })).toBe(
      "local",
    );
    expect(selectMode({ ...project, canCompile: false })).toBe("local");
    expect(selectMode(project)).toBe("project");
  });
  it("Validate compiles against the project and cancels a pending local check", async () => {
    const { service, local, compile } = harness(project);
    service.schedule("draft", "yaml");
    const outcome = await service.validateNow("draft", "yaml");
    await vi.advanceTimersByTimeAsync(2000);
    expect(compile).toHaveBeenCalledWith("draft", "yaml");
    expect(local).not.toHaveBeenCalled();
    expect(outcome).toMatchObject({ mode: "project", current: true });
    expect(service.snapshot).toMatchObject({ mode: "project", status: "done" });
  });
  it("Validate uses the local check offline", async () => {
    const { service, local, compile } = harness();
    const outcome = await service.validateNow("draft", "json");
    expect(local).toHaveBeenCalledWith("draft", "json");
    expect(compile).not.toHaveBeenCalled();
    expect(outcome.mode).toBe("local");
  });
  it("Validate reports failures to the caller", async () => {
    const { service, compile } = harness(project);
    compile.mockRejectedValueOnce(new Error("denied"));
    await expect(service.validateNow("x", "yaml")).rejects.toThrow("denied");
    expect(service.snapshot.status).toBe("failed");
  });
  it("Simulate reuses the compiled artifact instead of compiling twice", async () => {
    const { service, compile } = harness(project);
    await service.validateNow("flow", "yaml");
    const reused = await service.compile("flow", "yaml");
    expect(compile).toHaveBeenCalledTimes(1);
    expect(reused.artifact).toEqual({ digest: "sha256:4" });
    expect(service.cachedCompile("flow", "yaml")).toBe(reused);
    // A manual Validate always asks the platform again.
    await service.validateNow("flow", "yaml");
    expect(compile).toHaveBeenCalledTimes(2);
  });
  it("shares one in-flight compile and bounds the cache", async () => {
    const { service, compile, ctx } = harness(project, { cacheSize: 2 });
    const [a, b] = await Promise.all([
      service.compile("one", "yaml"),
      service.compile("one", "yaml"),
    ]);
    expect(a).toBe(b);
    expect(compile).toHaveBeenCalledTimes(1);
    await service.compile("two", "yaml");
    await service.compile("three", "yaml");
    expect(service.cachedCompile("one", "yaml")).toBeNull();
    expect(service.cachedCompile("three", "yaml")).not.toBeNull();
    ctx.scope = "/other/project";
    expect(service.cachedCompile("three", "yaml")).toBeNull();
    ctx.scope = project.scope;
    service.invalidate();
    expect(service.cachedCompile("three", "yaml")).toBeNull();
  });
  it("does not cache failed compiles and refuses to compile offline", async () => {
    const { service, compile } = harness(project);
    compile.mockRejectedValueOnce(new Error("timeout"));
    await expect(service.compile("x", "yaml")).rejects.toThrow("timeout");
    await service.compile("x", "yaml");
    expect(compile).toHaveBeenCalledTimes(2);
    const offline = harness();
    await expect(offline.service.compile("x", "yaml")).rejects.toThrow(
      /Connect to a platform/,
    );
    expect(offline.compile).not.toHaveBeenCalled();
  });
  it("gates publishing on a clean project compile of the exact source", async () => {
    const { service, compile, ctx } = harness(project);
    expect(service.canPublish("v1", "yaml")).toBe(false);
    expect(service.publishBlocker("v1", "yaml")).toBe(
      "Validate against the project catalog before publishing.",
    );
    await service.validateNow("v1", "yaml");
    expect(service.canPublish("v1", "yaml")).toBe(true);
    expect(service.publishBlocker("v1", "yaml")).toBe("");
    expect(service.canPublish("v2", "yaml")).toBe(false);
    compile.mockResolvedValueOnce({ ...failed, partial: false });
    await service.validateNow("v2", "yaml");
    expect(service.publishBlocker("v2", "yaml")).toBe(
      "Fix the errors found against the project catalog before publishing.",
    );
    compile.mockResolvedValueOnce(passed());
    await service.validateNow("v3", "yaml");
    expect(service.canPublish("v3", "yaml")).toBe(false);
    ctx.canCompile = false;
    expect(service.publishBlocker("v1", "yaml")).toBe(
      "Your account can't check workflows against the project catalog. Ask an administrator for compile access.",
    );
    ctx.connected = false;
    expect(service.publishBlocker("v1", "yaml")).toBe(
      "Connect to a platform to publish.",
    );
  });
  it("a compile that started before invalidate is neither shared nor cached", async () => {
    const { service, compile } = harness(project);
    const old = deferred<ValidationResult>();
    compile.mockImplementationOnce(() => old.promise);
    const first = service.compile("v1", "yaml");
    service.invalidate();
    const second = service.compile("v1", "yaml");
    old.resolve({ ...failed, partial: false });
    await expect(first).resolves.toMatchObject({ validationOk: false });
    await expect(second).resolves.toMatchObject({ ok: true });
    expect(compile).toHaveBeenCalledTimes(2);
    expect(service.cachedCompile("v1", "yaml")).toMatchObject({ ok: true });
    expect(service.canPublish("v1", "yaml")).toBe(true);
  });
  it("Validate is not current when the source changed while it ran", async () => {
    const { service, compile, ctx } = harness(project);
    const pending = deferred<ValidationResult>();
    compile.mockImplementationOnce(() => pending.promise);
    const outcome = service.validateNow("v1", "yaml");
    service.schedule("v2", "yaml");
    pending.resolve(compiled("v1"));
    await expect(outcome).resolves.toMatchObject({
      source: "v1",
      current: false,
    });
    expect(service.snapshot).toMatchObject({
      source: "v1",
      stale: true,
      status: "scheduled",
    });
    expect(summarize(service.snapshot, ctx).passedProjectCatalog).toBe(false);
    // The compiled artifact still serves Simulate and the publish gate.
    expect(service.canPublish("v1", "yaml")).toBe(true);
  });
});

describe("cancelled checks", () => {
  it("a cancelled check runs when the same source is scheduled again", async () => {
    const { service, local } = harness();
    service.schedule("v1", "yaml");
    await vi.advanceTimersByTimeAsync(700);
    service.schedule("v2", "yaml");
    service.cancel();
    expect(service.snapshot).toMatchObject({ source: "v1", stale: true });
    expect(service.schedule("v2", "yaml")).toBe(true);
    await vi.advanceTimersByTimeAsync(700);
    expect(local).toHaveBeenLastCalledWith("v2", "yaml");
    expect(service.snapshot).toMatchObject({
      source: "v2",
      stale: false,
      status: "done",
    });
  });
  it("a stale result left by cancel never reads as current", async () => {
    const project = { connected: true, canCompile: true, scope: "p" };
    const { service, ctx } = harness(project);
    await service.validateNow("v1", "yaml");
    service.schedule("v2", "yaml");
    service.cancel();
    expect(summarize(service.snapshot, ctx)).toMatchObject({
      tone: "idle",
      headline: "Validate to check your latest changes.",
      passedProjectCatalog: false,
      stale: true,
    });
  });
});

describe("banner copy", () => {
  it("counts errors and warnings for the summary line", () => {
    const list = [
      { severity: "error", message: "a" },
      { severity: "warning", message: "b" },
      { severity: "warning", message: "c" },
      { severity: "info", message: "d" },
      { message: "no severity counts as an error" },
    ];
    expect(countDiagnostics(list)).toEqual({
      errors: 2,
      warnings: 2,
      infos: 1,
    });
    expect(countLine(list)).toBe("2 errors, 2 warnings");
    expect(countLine([{ severity: "error", message: "x" }])).toBe("1 error");
    expect(countLine([{ severity: "warning", message: "x" }])).toBe(
      "1 warning",
    );
    expect(countLine([])).toBe("No problems");
  });
  it("claims a catalog pass only for a complete, successful project compile", async () => {
    const offline = harness();
    await offline.service.validateNow("x", "yaml");
    expect(summarize(offline.service.snapshot, offline.ctx)).toMatchObject({
      // "passed" is reserved for a complete project-catalog compile.
      tone: "checked",
      headline:
        "No problems found. Actions and connections are checked when you connect.",
      countLine: "No problems",
      passedProjectCatalog: false,
    });
    const connected = harness({
      connected: true,
      canCompile: true,
      scope: "p",
    });
    connected.local.mockResolvedValueOnce(passed());
    connected.service.schedule("y", "yaml");
    await vi.advanceTimersByTimeAsync(700);
    expect(summarize(connected.service.snapshot, connected.ctx).headline).toBe(
      "No problems found. Validate to check actions and connections against the project.",
    );
    await connected.service.validateNow("y", "yaml");
    expect(summarize(connected.service.snapshot, connected.ctx)).toMatchObject({
      tone: "passed",
      headline: "No problems found.",
      passedProjectCatalog: true,
    });
    connected.compile.mockResolvedValueOnce(passed({ partial: true }));
    await connected.service.validateNow("z", "yaml");
    expect(summarize(connected.service.snapshot, connected.ctx)).toMatchObject({
      tone: "checked",
      passedProjectCatalog: false,
      headline:
        "No errors found, but the project catalog check didn't finish. Validate again.",
    });
  });
  it("never claims a catalog pass for another project or while offline", async () => {
    const ctx = { connected: true, canCompile: true, scope: "p1" };
    const { service } = harness(ctx);
    await service.validateNow("v1", "yaml");
    expect(service.snapshot.scope).toBe("p1");
    expect(summarize(service.snapshot, ctx).passedProjectCatalog).toBe(true);
    for (const changed of [
      { ...ctx, scope: "p2" },
      { ...ctx, connected: false, scope: "" },
      { ...ctx, canCompile: false },
    ])
      expect(summarize(service.snapshot, changed)).toMatchObject({
        tone: "idle",
        headline: "Validate again to check your workflow.",
        passedProjectCatalog: false,
      });
    // While that new Validate runs, the banner says so.
    const { service: switching, ctx: now, compile } = harness(ctx);
    await switching.validateNow("v1", "yaml");
    now.scope = "p2";
    compile.mockImplementationOnce(() => new Promise(() => undefined));
    void switching.validateNow("v1", "yaml");
    expect(summarize(switching.snapshot, now)).toMatchObject({
      tone: "pending",
      headline: "Checking…",
    });
  });
  it("tells a connected author without compile access what Validate covers", async () => {
    const ctx = { connected: true, canCompile: false, scope: "p" };
    const { service } = harness(ctx);
    await service.validateNow("v1", "yaml");
    expect(summarize(service.snapshot, ctx)).toMatchObject({
      tone: "checked",
      headline:
        "No problems found. Your account can't check actions and connections against the project catalog.",
    });
  });
  it("checks locally and says to sign in again once the sign-in ended", async () => {
    const ctx = {
      connected: true,
      canCompile: false,
      signedOut: true,
      scope: "p",
    };
    const { service, compile } = harness(ctx);
    await service.validateNow("v1", "yaml");
    expect(compile).not.toHaveBeenCalled();
    expect(summarize(service.snapshot, ctx)).toMatchObject({
      tone: "checked",
      headline:
        "No problems found. Sign in again to check actions and connections against the project.",
    });
    expect(service.publishBlocker("v1", "yaml")).toBe(
      "Sign in again to publish.",
    );
  });
  it("counts every error the compiler reports, even when the list is truncated", async () => {
    const { service, local, ctx } = harness();
    local.mockResolvedValueOnce({ ...failed, errorCount: 5 });
    await service.validateNow("bad", "yaml");
    expect(summarize(service.snapshot, ctx)).toMatchObject({
      countLine: "5 errors, 1 warning",
      headline: "5 errors, 1 warning. Fix the errors to publish.",
    });
  });
  it("counts errors and warnings and never shows raw codes", async () => {
    const { service, local, ctx } = harness();
    local.mockResolvedValueOnce(failed);
    await service.validateNow("bad", "yaml");
    const summary = summarize(service.snapshot, ctx);
    expect(summary).toMatchObject({
      tone: "errors",
      countLine: "1 error, 1 warning",
      headline: "1 error, 1 warning. Fix the errors to publish.",
    });
    expect(JSON.stringify(summary)).not.toMatch(/WV-/);
    expect(summarize(harness().service.snapshot, ctx)).toMatchObject({
      // Nothing before the first result: no "Not validated".
      tone: "idle",
      headline: "",
      countLine: "",
    });
  });
  it("describes pending and failed checks", async () => {
    const { service, local, ctx } = harness();
    service.schedule("a", "yaml");
    expect(summarize(service.snapshot, ctx)).toMatchObject({
      tone: "pending",
      headline: "Checking…",
    });
    local.mockRejectedValueOnce(new Error("offline"));
    await vi.advanceTimersByTimeAsync(700);
    expect(summarize(service.snapshot, ctx)).toMatchObject({
      tone: "failed",
      headline: "Studio couldn't check this workflow. Try again.",
    });
  });
});
