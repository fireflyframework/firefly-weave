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
import { describeResponse, PlainError } from "./errors";
export interface Profile {
  name: string;
  baseUrl: string;
  tenantId: string | null;
  projectId: string | null;
  environmentId: string | null;
}
export interface Session {
  paired: boolean;
  csrfToken?: string;
  version: string;
  mode: "offline" | "connected";
  profile: Profile | null;
  connection?: { configured?: boolean; login_supported?: boolean };
}
export interface Diagnostic {
  message: string;
  code?: string;
  severity?: string;
  [key: string]: unknown;
}
export interface Validation {
  validationOk: boolean;
  errorCount: number;
  diagnostics: Diagnostic[];
  partial?: boolean;
  artifact?: unknown;
}
export class ApiError extends Error {
  readonly plain: PlainError;
  constructor(
    public status: number,
    public detail: unknown,
  ) {
    const plain = describeResponse(status, detail);
    super(plain.message);
    this.plain = plain;
  }
  get code() {
    return this.plain.code;
  }
}
/** The platform's admission rejections: nothing ran, so the request may be sent again. */
const capacityCodes = new Set(["WV-OPERATION-CAPACITY", "WV-REQUEST-CAPACITY"]);
const capacityAttempts = 4;
/** `Retry-After` in seconds, kept between a quarter second and five seconds. */
const retryDelay = (header: string | null) => {
  const seconds = Number(header);
  return Number.isFinite(seconds) && header !== null && header.trim() !== ""
    ? Math.min(Math.max(seconds, 0.25), 5) * 1000
    : 1000;
};
export class StudioApi {
  session: Session = {
    paired: false,
    version: "",
    mode: "offline",
    profile: null,
  };
  /** Called when the local host no longer recognizes this window's pairing. */
  onSessionEnded: (() => void) | null = null;
  /** Pauses before replaying a request the platform turned away for capacity. */
  wait = (milliseconds: number) =>
    new Promise<void>((resolve) => setTimeout(resolve, milliseconds));
  /**
   * Calls the paired local host. `timeout` (milliseconds) bounds the wait;
   * platform checks that reach a remote server or identity provider pass more.
   */
  async request<T>(
    path: string,
    method = "GET",
    body?: unknown,
    headers: Record<string, string> = {},
    timeout = 15000,
  ): Promise<T> {
    if (!path.startsWith("/studio/") || path.startsWith("//"))
      throw Error("Studio requests must use the same-origin host boundary.");
    const h: Record<string, string> = { ...headers };
    if (body !== undefined) h["Content-Type"] = "application/json";
    if (method !== "GET" && this.session.csrfToken)
      h["X-Weave-CSRF"] = this.session.csrfToken;
    // A capacity rejection means the platform did not admit the request, so a
    // read, or a change that carries an idempotency key, can be sent again.
    const replayable = method === "GET" || "Idempotency-Key" in h;
    for (let attempt = 1; ; attempt++) {
      const response = await fetch(path, {
        method,
        credentials: "same-origin",
        headers: h,
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: AbortSignal.timeout(timeout),
      });
      const value: unknown = await response
        .json()
        .catch(() => ({ message: `HTTP ${response.status}` }));
      if (response.ok) return value as T;
      const error = new ApiError(response.status, value);
      if (
        replayable &&
        attempt < capacityAttempts &&
        response.status === 429 &&
        capacityCodes.has(error.code)
      ) {
        await this.wait(retryDelay(response.headers.get("Retry-After")));
        continue;
      }
      if (
        response.status === 401 &&
        error.code === "WV-STUDIO-SESSION" &&
        path !== "/studio/session"
      )
        this.onSessionEnded?.();
      throw error;
    }
  }
  /** Takes a session payload from a connection change, keeping this window's CSRF token. */
  adopt(session: Session | null | undefined) {
    if (!session || typeof session !== "object" || !("paired" in session))
      return this.session;
    return (this.session = {
      ...session,
      csrfToken: session.csrfToken ?? this.session.csrfToken,
    });
  }
  async pair(code?: string) {
    return (this.session = await this.request<Session>(
      "/studio/session",
      code === undefined ? "GET" : "POST",
      code === undefined ? undefined : { code },
    ));
  }
  get project() {
    const p = this.session.profile;
    if (!p?.tenantId || !p.projectId)
      throw Error("Choose an authorized workspace before this action.");
    return `/studio/api/api/v1/tenants/${encodeURIComponent(p.tenantId)}/projects/${encodeURIComponent(p.projectId)}`;
  }
  get environment() {
    const p = this.session.profile;
    if (!p) throw Error("Choose an environment.");
    if (!p.environmentId)
      throw Error("Choose an authorized environment before this action.");
    return `${this.project}/environments/${encodeURIComponent(p.environmentId)}`;
  }
  validate(source: string, format: string) {
    return this.request<Validation>("/studio/local/validate", "POST", {
      source,
      format,
    });
  }
  async page(
    collection: string,
    environment = false,
    cursor?: string,
    filters: Record<string, string> = {},
  ) {
    return this.request<{
      items: Record<string, unknown>[];
      next_cursor: string | null;
    }>(
      `${environment ? this.environment : this.project}/${collection}?limit=50${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}${Object.entries(
        filters,
      )
        .filter(([, value]) => value)
        .map(
          ([key, value]) =>
            `&${encodeURIComponent(key)}=${encodeURIComponent(value)}`,
        )
        .join("")}`,
    );
  }
  mutate<T>(
    path: string,
    method: string,
    body: unknown,
    revision?: number,
    key: string = crypto.randomUUID(),
  ) {
    const headers: Record<string, string> = { "Idempotency-Key": key };
    if (revision !== undefined) headers["If-Match"] = `"${revision}"`;
    return this.request<T>(path, method, body, headers);
  }
}
