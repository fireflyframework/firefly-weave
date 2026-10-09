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
// Actions a workflow owns: Call an API, Send email, Reply to an email and
// the file steps create one Action document per step, named after the
// workflow and the step, and edited from the step. A recipe says what the
// action does; its document is derived (the host builds HTTP actions; email
// and file actions copy a built-in connector action).
import { decode, get } from "../../../forms/core/binding";
import { getAt, isJsonObject } from "../../../forms/core/json";
import type { Step } from "../../../model";
import { uniqueIdentifier } from "../params/identifiers";
import type { Json, KindContext } from "../registry";

export const HTTP_CONNECTOR = "weave-http@2.0.0";
export const EMAIL_CONNECTOR = "weave-email@1.0.0";
export const OWNED_VERSION = "1.0.0";
export type HttpMethod = "GET" | "HEAD" | "POST" | "PUT" | "PATCH" | "DELETE";
export interface RetrySettings {
  maxAttempts: number;
  initialDelaySeconds: number;
  maxDelaySeconds: number;
}
export const DEFAULT_RETRY: RetrySettings = {
  maxAttempts: 3,
  initialDelaySeconds: 1,
  maxDelaySeconds: 30,
};
export interface HttpRecipe {
  kind: "http";
  method: HttpMethod;
  /** The path after the connection's address, with {name} placeholders. */
  pathTemplate: string;
  responseSample?: Json;
  statuses: number[];
  description?: string;
  timeoutSeconds: number;
  retry?: RetrySettings;
}
export interface ConnectorRecipe {
  kind: "connector";
  /** "weave-email@1.0.0", "weave-sftp@1.0.0". */
  connector: string;
  /** "send", "reply", "list", "write"… */
  action: string;
  timeoutSeconds?: number;
  retry?: RetrySettings;
  description?: string;
}
export type OwnedRecipe = HttpRecipe | ConnectorRecipe;

export const newHttpRecipe = (): HttpRecipe => ({
  kind: "http",
  method: "GET",
  pathTemplate: "",
  statuses: [200],
  timeoutSeconds: 30,
});
export const newConnectorRecipe = (
  connector: string,
  action: string,
): ConnectorRecipe => ({
  kind: "connector",
  connector,
  action,
});

/** `<workflow>.<step>`, at most 128 characters, `-2` when taken. */
export function ownedName(
  workflow: string,
  step: string,
  taken: ReadonlySet<string>,
): string {
  return uniqueIdentifier(
    `${workflow}.${step}`.slice(0, 128).replace(/[.-]+$/, ""),
    taken,
  );
}
export const ownedUses = (name: string): string => `${name}@${OWNED_VERSION}`;
export const nameOfUses = (uses: string): string => {
  const at = uses.lastIndexOf("@");
  return at > 0 ? uses.slice(0, at) : uses;
};
/** Changes data on the other side: anything but GET and HEAD. */
export const isWriteMethod = (method: string): boolean =>
  method !== "GET" && method !== "HEAD";
/** Carries a request body: POST, PUT and PATCH. A DELETE changes data but sends none. */
export const sendsBody = (method: string): boolean =>
  method === "POST" || method === "PUT" || method === "PATCH";

export function pathPlaceholders(path: string): string[] {
  return [...path.matchAll(/\{([A-Za-z_][A-Za-z0-9_-]{0,127})\}/g)].map(
    (match) => match[1],
  );
}

/**
 * The keys of one part (`query`, `headers`, `body`) of the step's input. A
 * part that isn't an object with keys of its own (missing, mapped as a whole,
 * or a formula) has none. There is no failure to catch here: `decode` turns
 * whatever it can't read into a formula node, which has no keys, so an
 * exception would be a real fault and must reach the caller.
 */
function partKeys(stepWith: unknown, part: string): string[] {
  const node = get(decode(stepWith), [part]);
  return node?.kind === "object" ? Object.keys(node.entries) : [];
}

/**
 * The host builder's request (`POST /studio/local/http-action`), or null
 * while the path can't be built yet (empty, or not starting with "/").
 * Query and header names come from the step's input keys; for POST, PUT and
 * PATCH the body's fields do too, with any value.
 */
export function httpBuildRequest(
  name: string,
  recipe: HttpRecipe,
  stepWith: unknown,
): Record<string, unknown> | null {
  const path = recipe.pathTemplate.trim();
  if (!path.startsWith("/") || path.startsWith("//")) return null;
  const parameters = [
    ...partKeys(stepWith, "query").map((key) => ({
      name: key,
      location: "query",
      type: "string",
    })),
    ...partKeys(stepWith, "headers").map((key) => ({
      name: key,
      location: "header",
      type: "string",
    })),
  ];
  const request: Record<string, unknown> = {
    name,
    version: OWNED_VERSION,
    method: recipe.method,
    pathTemplate: path,
    parameters,
    statuses: recipe.statuses.length ? recipe.statuses : [200],
    timeoutSeconds: recipe.timeoutSeconds,
  };
  if (recipe.description) request["description"] = recipe.description;
  if (recipe.responseSample !== undefined)
    request["responseSample"] = recipe.responseSample;
  const body = partKeys(stepWith, "body");
  if (sendsBody(recipe.method) && body.length)
    request["bodySchema"] = {
      type: "object",
      properties: Object.fromEntries(body.map((key) => [key, {}])),
      required: body,
      additionalProperties: false,
    };
  return request;
}

/** A built-in connector action copied as this workflow's own document. */
export function connectorDocument(
  name: string,
  recipe: ConnectorRecipe,
  template: Json,
): Json {
  const copy = structuredClone(template) as {
    metadata: Record<string, Json>;
    spec: Record<string, Json>;
  };
  copy.metadata = { name, version: OWNED_VERSION };
  if (recipe.timeoutSeconds !== undefined)
    copy.spec["timeoutSeconds"] = recipe.timeoutSeconds;
  // Actions carry their description in the input schema, as the HTTP builder writes it.
  if (recipe.description)
    copy.spec["inputSchema"] = {
      ...((copy.spec["inputSchema"] ?? {}) as Record<string, Json>),
      description: recipe.description,
    };
  return withRetry(copy as unknown as Json, recipe.retry);
}

export const sideEffectOf = (document: Json | null): string =>
  String(getAt(document, ["spec", "sideEffect"]) ?? "");

/** The document with `spec.retry`, only for actions Weave may repeat (they read data or are idempotent). */
export function withRetry(
  document: Json,
  retry: RetrySettings | undefined,
): Json {
  if (
    !retry ||
    sideEffectOf(document) === "non_idempotent" ||
    !isJsonObject(getAt(document, ["spec"]))
  )
    return document;
  const copy = structuredClone(document) as { spec: Record<string, Json> };
  copy.spec["retry"] = { ...retry };
  return copy as unknown as Json;
}

/** The recipe of the action this step calls, when the workflow owns it. */
export function recipeOf(step: Step, ctx: KindContext): OwnedRecipe | null {
  const recipe = ctx.ownedAction?.(String(step["uses"] ?? ""));
  return recipe && isJsonObject(recipe)
    ? (recipe as unknown as OwnedRecipe)
    : null;
}
