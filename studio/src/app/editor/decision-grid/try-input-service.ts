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
// Try an input: Studio's live answer at once (each rule's mark and the
// table's answer), then Weave's own check on the Studio host 400 ms after the
// last change. An answer applies only to the table revision and the input it
// was asked for. What is tried stays in memory: never stored, never logged.
import { signal } from "@angular/core";
import type { Json } from "../../forms/core/json";
import type { DecisionEvaluation } from "../state/execution";
import type { TableDocument } from "../state/table-drafts";
import { tryTable, type RowMark, type TableAnswer } from "./table-evaluate";

export interface TryRequest {
  table: TableDocument;
  input: Json;
}
export interface TryRefusal {
  code: string;
  message: string;
  path: string;
  /** Rule numbers (from 1) the refusal is about. */
  rules: number[];
}
export type TryChecked =
  | { answer: DecisionEvaluation }
  | { refused: TryRefusal }
  | "checking"
  | "offline-host"
  | null;
export interface TryState {
  marks: RowMark[];
  live: TableAnswer | null;
  checked: TryChecked;
  revision: number;
}

export const CHECK_DELAY = 400;
export const UNREACHABLE =
  "Studio couldn't reach its own host; the live answer is shown.";
const MULTIPLE = "WV-DECISION-MULTIPLE_MATCHES";
const PREDICATE = "WV-DECISION-PREDICATE";

const ruleOf = (path: string): number | null => {
  const found = /^\/spec\/rules\/(\d+)\//.exec(path);
  return found ? Number(found[1]) + 1 : null;
};
const list = (numbers: number[]) =>
  numbers.length <= 2
    ? numbers.join(" and ")
    : `${numbers.slice(0, -1).join(", ")} and ${numbers.at(-1)}`;

/** A refusal in words, with the rules it is about. */
export function explainRefusal(
  refusal: { code: string; message: string; path: string },
  marks: readonly RowMark[],
): { message: string; rules: number[] } {
  if (refusal.code === MULTIPLE) {
    const matched = marks.flatMap((mark, index) =>
      mark === "match" ? [index + 1] : [],
    );
    const named = ruleOf(refusal.path);
    const rules = [...new Set([...matched, ...(named ? [named] : [])])].sort(
      (a, b) => a - b,
    );
    return rules.length >= 2
      ? {
          message: `${refusal.message.replace(/\.$/, "")} (rules ${list(rules)}).`,
          rules,
        }
      : { message: refusal.message, rules };
  }
  if (refusal.code === PREDICATE) {
    const rule = ruleOf(refusal.path);
    if (rule)
      return {
        message: `Rule ${rule}'s condition isn't true or false.`,
        rules: [rule],
      };
  }
  const rule = ruleOf(refusal.path);
  return { message: refusal.message, rules: rule ? [rule] : [] };
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
/** The refusal a host error carries; null for anything that isn't one. */
function refusalOf(
  error: unknown,
): { status: number; code: string; message: string; path: string } | null {
  if (!isRecord(error) || typeof error["status"] !== "number") return null;
  const detail = isRecord(error["detail"]) ? error["detail"] : {};
  const result = isRecord(detail["result"]) ? detail["result"] : {};
  return {
    status: error["status"],
    code: String(detail["code"] ?? error["code"] ?? ""),
    message: String(detail["message"] ?? ""),
    path: typeof result["path"] === "string" ? result["path"] : "",
  };
}

const same = (a: unknown, b: unknown) =>
  JSON.stringify(a) === JSON.stringify(b);

export class TryInputService {
  readonly state = signal<TryState>({
    marks: [],
    live: null,
    checked: null,
    revision: 0,
  });
  /** Bumped by every change: an answer asked for before never lands. */
  private generation = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;

  constructor(
    private readonly post: (body: TryRequest) => Promise<unknown>,
    private readonly options: {
      delay?: number;
      warn?: (message: string, data: { code: string }) => void;
    } = {},
  ) {}

  /** A new input or a new table: the live answer now, Weave's check soon. */
  set(table: TableDocument, input: Json, revision: number): void {
    const tried = tryTable(table.spec, input);
    this.generation++;
    this.state.set({
      marks: tried.marks,
      live: tried.answer,
      checked: "checking",
      revision,
    });
    this.schedule(this.generation, { table, input }, tried.answer, true);
  }

  /** Forgets what was tried (closing the panel). */
  clear(): void {
    this.generation++;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    this.state.set({ marks: [], live: null, checked: null, revision: 0 });
  }

  private schedule(
    generation: number,
    body: TryRequest,
    live: TableAnswer,
    retry: boolean,
  ) {
    if (this.timer) clearTimeout(this.timer);
    this.timer = setTimeout(
      () => void this.check(generation, body, live, retry),
      this.options.delay ?? CHECK_DELAY,
    );
  }

  private async check(
    generation: number,
    body: TryRequest,
    live: TableAnswer,
    retry: boolean,
  ): Promise<void> {
    this.timer = null;
    let answer: unknown;
    try {
      answer = await this.post(body);
    } catch (error) {
      if (generation !== this.generation) return;
      const refusal = refusalOf(error);
      if (!refusal) return this.settle("offline-host");
      if (refusal.status === 503 && refusal.code === "WV-STUDIO-BUSY" && retry)
        return this.schedule(generation, body, live, false);
      if (refusal.status !== 422) return this.settle("offline-host");
      if (live.ok || live.code !== refusal.code) this.disagree(refusal.code);
      const words = explainRefusal(refusal, this.state().marks);
      return this.settle({
        refused: {
          code: refusal.code,
          message: words.message,
          path: refusal.path,
          rules: words.rules,
        },
      });
    }
    if (generation !== this.generation) return;
    const evaluation = answer as DecisionEvaluation;
    if (
      !live.ok ||
      !same(live.output, evaluation.output) ||
      !same(live.matched, evaluation.matched_rule_ids)
    )
      this.disagree(live.ok ? "WV-DECISION-ANSWER" : live.code);
    this.settle({ answer: evaluation });
  }

  private settle(checked: TryChecked) {
    this.state.update((state) => ({ ...state, checked }));
  }
  private disagree(code: string) {
    this.options.warn?.("Studio and Weave answered this input differently", {
      code,
    });
  }
}
