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
// Live validation for the designer, free of DOM and Angular:
// - every source change schedules a host-local check (`/studio/local/validate`)
//   after 600–800 ms of quiet; unchanged source (layout-only edits) is skipped
//   and keeps its diagnostics;
// - a sequence guard applies only the newest answer;
// - the manual Validate compiles against the project catalog
//   (`{project}/compiler/compile`) when connected with `compile`, otherwise it
//   runs the local check;
// - compile results are cached per project, format and source, so Simulate
//   and the publish gate reuse the artifact instead of compiling twice.

export type SourceFormat = "yaml" | "json";
export type ValidationMode = "local" | "project";

/** The compiler result body; structurally the `Validation` of `api.ts`. */
export interface ValidationResult {
  validationOk: boolean;
  errorCount: number;
  diagnostics: {
    message: string;
    code?: string;
    severity?: string;
    path?: string;
    [key: string]: unknown;
  }[];
  partial?: boolean;
  ok?: boolean;
  artifact?: unknown;
}

export interface ValidationTransport {
  /** Host-local authoring validation; never leaves this computer. */
  local(source: string, format: SourceFormat): Promise<ValidationResult>;
  /** Full compile against the connected project's catalog. */
  compile(source: string, format: SourceFormat): Promise<ValidationResult>;
}

export interface ValidationContext {
  /** A platform profile with an authorized project is active. */
  connected: boolean;
  /** The signed-in identity holds the `compile` capability there. */
  canCompile: boolean;
  /** The platform's sign-in ended (or the account isn't linked) until signing in again. */
  signedOut?: boolean;
  /** Identifies the project whose catalog compiles depend on. */
  scope: string;
}

export interface ValidationSnapshot {
  status: "idle" | "scheduled" | "running" | "done" | "failed";
  /** How `result` was produced. */
  mode: ValidationMode | null;
  /** The source and format `result` belongs to. */
  source: string | null;
  format: SourceFormat | null;
  result: ValidationResult | null;
  /** The context `scope` (project) `result` was produced in. */
  scope: string | null;
  /** The latest failure, until the next successful check. */
  error: unknown;
  /** True while `result` describes an older source than the latest requested. */
  stale: boolean;
}

export interface ValidationOutcome {
  mode: ValidationMode;
  source: string;
  format: SourceFormat;
  result: ValidationResult;
  /**
   * False when a newer request (a later Validate, or a change scheduled while
   * this one ran) superseded it; the snapshot then marks the result stale.
   */
  current: boolean;
}

export interface ValidationServiceOptions {
  transport: ValidationTransport;
  context: () => ValidationContext;
  /** Called after every snapshot change; components call `markForCheck()`. */
  onChange?: (snapshot: ValidationSnapshot) => void;
  /** Quiet period before a local check, clamped to 600–800 ms (default 700). */
  delay?: number;
  /** Compile results kept for reuse (default 4). */
  cacheSize?: number;
}

export function selectMode(context: ValidationContext): ValidationMode {
  return context.connected && context.canCompile ? "project" : "local";
}

const MIN_DELAY = 600;
const MAX_DELAY = 800;

export class ValidationService {
  private readonly delay: number;
  private readonly cacheSize: number;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private sequence = 0;
  private running = 0;
  private requested: { source: string; format: SourceFormat } | null = null;
  /** A check of `requested` is queued, running or done (not cancelled or failed). */
  private covered = false;
  private outcome: "none" | "done" | "failed" = "none";
  private state: Omit<ValidationSnapshot, "status" | "stale"> = {
    mode: null,
    source: null,
    format: null,
    result: null,
    scope: null,
    error: undefined,
  };
  private readonly cache = new Map<string, ValidationResult>();
  private readonly inflight = new Map<string, Promise<ValidationResult>>();
  /** Bumped by `invalidate()`; compiles started earlier are not cached. */
  private generation = 0;
  private disposed = false;

  constructor(private readonly options: ValidationServiceOptions) {
    const delay = options.delay ?? 700;
    this.delay = Math.min(MAX_DELAY, Math.max(MIN_DELAY, delay));
    this.cacheSize = Math.max(1, options.cacheSize ?? 4);
  }

  get snapshot(): ValidationSnapshot {
    const { source, format } = this.state;
    return {
      ...this.state,
      status: this.timer
        ? "scheduled"
        : this.running
          ? "running"
          : this.outcome === "none"
            ? "idle"
            : this.outcome,
      stale:
        this.requested !== null &&
        source !== null &&
        (source !== this.requested.source || format !== this.requested.format),
    };
  }

  /** True when the snapshot's result was produced for exactly this source. */
  hasResultFor(source: string, format: SourceFormat): boolean {
    return (
      this.state.result !== null &&
      this.state.source === source &&
      this.state.format === format
    );
  }

  /**
   * Queues a local check of `source` after the quiet period. Returns false
   * when the source is unchanged (a layout-only edit), keeping diagnostics.
   */
  schedule(source: string, format: SourceFormat): boolean {
    if (this.disposed) return false;
    if (this.covered && this.isRequested(source, format)) return false;
    this.requested = { source, format };
    this.covered = true;
    this.clearTimer();
    this.timer = setTimeout(() => {
      this.timer = null;
      void this.run("local", source, format).catch(() => undefined);
    }, this.delay);
    this.emit();
    return true;
  }

  /**
   * The Validate command: a fresh project compile when connected with
   * `compile`, otherwise the local check. Rejects when the check fails.
   */
  validateNow(
    source: string,
    format: SourceFormat,
  ): Promise<ValidationOutcome> {
    this.requested = { source, format };
    this.covered = true;
    this.clearTimer();
    return this.run(selectMode(this.options.context()), source, format, true);
  }

  /** A project compile for `source`, reusing a cached or in-flight result. */
  compile(source: string, format: SourceFormat): Promise<ValidationResult> {
    return this.projectCompile(source, format, false);
  }

  /** The cached project compile of exactly this source, if any. */
  cachedCompile(source: string, format: SourceFormat): ValidationResult | null {
    return this.cache.get(this.key(source, format)) ?? null;
  }

  /** "" when `source` may be published; otherwise why not, in plain words. */
  publishBlocker(source: string, format: SourceFormat): string {
    const context = this.options.context();
    if (!context.connected) return "Connect to a platform to publish.";
    if (context.signedOut) return "Sign in again to publish.";
    if (!context.canCompile)
      return "Your account can't check workflows against the project catalog. Ask an administrator for compile access.";
    const result = this.cachedCompile(source, format);
    if (!result)
      return "Validate against the project catalog before publishing.";
    if (!result.validationOk || result.errorCount > 0)
      return "Fix the errors found against the project catalog before publishing.";
    if (!result.ok || result.partial)
      return "Validate against the project catalog before publishing.";
    return "";
  }

  canPublish(source: string, format: SourceFormat): boolean {
    return this.publishBlocker(source, format) === "";
  }

  /**
   * Forget compile results, e.g. after the project catalog changed. Compiles
   * still in flight finish for their callers but are neither cached nor shared.
   */
  invalidate(): void {
    this.generation++;
    this.cache.clear();
    this.inflight.clear();
  }

  /**
   * Drop a pending local check without touching the current result. The
   * result stays stale, and scheduling the same source again re-queues it.
   */
  cancel(): void {
    if (!this.timer) return;
    this.clearTimer();
    this.covered = false;
    this.emit();
  }

  dispose(): void {
    this.disposed = true;
    this.clearTimer();
    this.inflight.clear();
  }

  private isRequested(source: string, format: SourceFormat) {
    return (
      this.requested?.source === source && this.requested.format === format
    );
  }

  private key(source: string, format: SourceFormat) {
    return `${this.options.context().scope}\u0000${format}\u0000${source}`;
  }

  private projectCompile(
    source: string,
    format: SourceFormat,
    fresh: boolean,
  ): Promise<ValidationResult> {
    const context = this.options.context();
    if (!context.connected || !context.canCompile)
      return Promise.reject(
        Error(
          "Connect to a platform with compile access to check against the project catalog.",
        ),
      );
    const key = this.key(source, format);
    const cached = this.cache.get(key);
    if (cached && !fresh) {
      this.cache.delete(key);
      this.cache.set(key, cached);
      return Promise.resolve(cached);
    }
    const pending = this.inflight.get(key);
    if (pending) return pending;
    const generation = this.generation;
    const request = this.options.transport
      .compile(source, format)
      .then((result) => {
        if (this.disposed || generation !== this.generation) return result;
        this.cache.delete(key);
        this.cache.set(key, result);
        while (this.cache.size > this.cacheSize)
          this.cache.delete(this.cache.keys().next().value as string);
        return result;
      })
      .finally(() => {
        if (this.inflight.get(key) === request) this.inflight.delete(key);
      });
    this.inflight.set(key, request);
    return request;
  }

  private async run(
    mode: ValidationMode,
    source: string,
    format: SourceFormat,
    fresh = false,
  ): Promise<ValidationOutcome> {
    const sequence = ++this.sequence;
    const scope = this.options.context().scope;
    this.running = sequence;
    this.emit();
    try {
      const result =
        mode === "project"
          ? await this.projectCompile(source, format, fresh)
          : await this.options.transport.local(source, format);
      const applied = sequence === this.sequence && !this.disposed;
      if (applied) {
        this.running = 0;
        this.outcome = "done";
        this.state = { mode, source, format, result, scope, error: undefined };
        this.emit();
      }
      const current = applied && this.isRequested(source, format);
      return { mode, source, format, result, current };
    } catch (error) {
      if (sequence === this.sequence && !this.disposed) {
        this.running = 0;
        this.outcome = "failed";
        if (this.isRequested(source, format)) this.covered = false;
        this.state = { ...this.state, error };
        this.emit();
      }
      throw error;
    }
  }

  private clearTimer() {
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
  }

  private emit() {
    if (!this.disposed) this.options.onChange?.(this.snapshot);
  }
}

export interface DiagnosticCounts {
  errors: number;
  warnings: number;
  infos: number;
}

export function countDiagnostics<T extends { severity?: unknown }>(
  diagnostics: readonly T[],
): DiagnosticCounts {
  const counts = { errors: 0, warnings: 0, infos: 0 };
  for (const d of diagnostics)
    if (d.severity === "warning") counts.warnings++;
    else if (d.severity === "info") counts.infos++;
    else counts.errors++;
  return counts;
}

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

/** "2 errors, 1 warning", "1 warning" or "No problems". */
export function countLine<T extends { severity?: unknown }>(
  diagnostics: readonly T[] | DiagnosticCounts,
): string {
  const counts = Array.isArray(diagnostics)
    ? countDiagnostics(diagnostics)
    : (diagnostics as DiagnosticCounts);
  const parts = [
    counts.errors ? plural(counts.errors, "error") : "",
    counts.warnings ? plural(counts.warnings, "warning") : "",
  ].filter(Boolean);
  return parts.length ? parts.join(", ") : "No problems";
}

export interface ValidationSummary {
  /**
   * `passed` only for a complete, error-free project-catalog compile;
   * `checked` for any other result without errors or warnings.
   */
  tone:
    | "idle"
    | "pending"
    | "passed"
    | "checked"
    | "warnings"
    | "errors"
    | "failed";
  headline: string;
  /** "N errors · M warnings"; "" before any result. */
  countLine: string;
  /** Only a complete, error-free compile against the project catalog. */
  passedProjectCatalog: boolean;
  /** The result describes an older version of the source. */
  stale: boolean;
}

/**
 * The diagnostics strip's one status line: "Checking…", "No problems
 * found." (with what a local check leaves out), or "2 errors, 1 warning.
 * Fix the errors to publish."
 */
export function summarize(
  snapshot: ValidationSnapshot,
  context: ValidationContext,
): ValidationSummary {
  const { result, mode, status, stale } = snapshot;
  const listed = result ? countDiagnostics(result.diagnostics) : null;
  // A truncated list can hold fewer errors than the compiler counted.
  const counts = listed && {
    ...listed,
    errors: Math.max(result!.errorCount, listed.errors),
  };
  const base = {
    countLine: counts ? countLine(counts) : "",
    passedProjectCatalog: false,
    stale,
  };
  if (status === "failed")
    return {
      ...base,
      tone: "failed",
      headline: "Studio couldn't check this workflow. Try again.",
    };
  if (!result || (stale && status !== "done"))
    return status === "idle"
      ? { ...base, tone: "idle", headline: "" }
      : { ...base, tone: "pending", headline: "Checking…" };
  if (stale)
    return {
      ...base,
      tone: "idle",
      headline: "Validate to check your latest changes.",
    };
  if (
    mode === "project" &&
    (snapshot.scope !== context.scope || selectMode(context) !== "project")
  )
    // Checked against another project, or before access or the connection changed.
    return status === "running"
      ? { ...base, tone: "pending", headline: "Checking…" }
      : {
          ...base,
          tone: "idle",
          headline: "Validate again to check your workflow.",
        };
  if (counts!.errors > 0 || !result.validationOk)
    return {
      ...base,
      tone: "errors",
      headline: `${countLine({ ...counts!, errors: Math.max(1, counts!.errors) })}. Fix the errors to publish.`,
    };
  const warned = counts!.warnings > 0;
  const found = warned
    ? `No errors, ${plural(counts!.warnings, "warning")}.`
    : "No problems found.";
  if (mode === "project") {
    const complete = result.ok === true && !result.partial;
    return {
      ...base,
      tone: warned ? "warnings" : complete ? "passed" : "checked",
      passedProjectCatalog: complete,
      headline: complete
        ? found
        : "No errors found, but the project catalog check didn't finish. Validate again.",
    };
  }
  const coverage =
    selectMode(context) === "project"
      ? "Validate to check actions and connections against the project."
      : context.connected
        ? context.signedOut
          ? "Sign in again to check actions and connections against the project."
          : "Your account can't check actions and connections against the project catalog."
        : "Actions and connections are checked when you connect.";
  return {
    ...base,
    tone: warned ? "warnings" : "checked",
    headline: `${found} ${coverage}`,
  };
}
