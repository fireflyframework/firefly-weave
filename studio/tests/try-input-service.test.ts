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
  TryInputService,
  explainRefusal,
  type TryRequest,
} from "../src/app/editor/decision-grid/try-input-service";
import type { TableDocument } from "../src/app/editor/state/table-drafts";

const ref = (name: string) => ({ ref: `/input/${name}` });
const op = (name: string, ...args: unknown[]) => ({ op: { name, args } });
const lit = (value: unknown) => ({ literal: value });
const table = (policy: "first" | "unique" = "first"): TableDocument => ({
  apiVersion: "weave/v1alpha1",
  kind: "DecisionTable",
  metadata: { name: "payment-policy", version: "1.2.1" },
  spec: {
    inputSchema: {
      type: "object",
      properties: { amount: { type: "number" }, tier: { type: "string" } },
    },
    outputSchema: {
      type: "object",
      properties: { approve: { type: "boolean" } },
    },
    hitPolicy: policy,
    rules: [
      {
        id: "big",
        when: op("gt", ref("amount"), lit(1000)),
        output: lit({ approve: true }),
      },
      {
        id: "small",
        when: op("lte", ref("amount"), lit(1000)),
        output: lit({ approve: false }),
      },
      {
        id: "gold",
        when: op("eq", ref("tier"), lit("gold")),
        output: lit({ approve: true }),
      },
    ],
  },
});
const failure = (
  status: number,
  code: string,
  result: unknown = {},
  message = code,
) =>
  Object.assign(new Error(message), {
    status,
    code,
    detail: { status, code, message, result },
  });

describe("Try an input", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("answers at once and checks with Weave 400 ms after the last change", async () => {
    const asked: TryRequest[] = [];
    const service = new TryInputService(async (body) => {
      asked.push(body);
      return {
        output: { approve: true },
        matched_rule_ids: ["big"],
        used_default: false,
      };
    });
    service.set(table(), { amount: 2000, tier: "silver" }, 1);
    expect(service.state().marks).toEqual(["match", "no-match", "no-match"]);
    expect(service.state().live).toMatchObject({ ok: true, matched: ["big"] });
    expect(service.state().checked).toBe("checking");
    await vi.advanceTimersByTimeAsync(300);
    service.set(table(), { amount: 3000, tier: "silver" }, 1);
    await vi.advanceTimersByTimeAsync(300);
    expect(asked).toHaveLength(0);
    await vi.advanceTimersByTimeAsync(100);
    expect(asked).toEqual([
      { table: table(), input: { amount: 3000, tier: "silver" } },
    ]);
    expect(service.state().checked).toEqual({
      answer: {
        output: { approve: true },
        matched_rule_ids: ["big"],
        used_default: false,
      },
    });
  });

  it("applies an answer only to the table revision it was asked for", async () => {
    let release!: (value: unknown) => void;
    const answers: Promise<unknown>[] = [
      new Promise((resolve) => (release = resolve)),
      Promise.resolve({
        output: { approve: false },
        matched_rule_ids: ["small"],
        used_default: false,
      }),
    ];
    let call = 0;
    const service = new TryInputService(() => answers[call++]);
    service.set(table(), { amount: 2000 }, 1);
    await vi.advanceTimersByTimeAsync(400);
    // The table changes while Weave checks.
    service.set(table(), { amount: 500 }, 2);
    release({
      output: { approve: true },
      matched_rule_ids: ["big"],
      used_default: false,
    });
    await vi.advanceTimersByTimeAsync(0);
    expect(service.state().checked).toBe("checking");
    await vi.advanceTimersByTimeAsync(400);
    expect(service.state().revision).toBe(2);
    expect(service.state().checked).toEqual({
      answer: {
        output: { approve: false },
        matched_rule_ids: ["small"],
        used_default: false,
      },
    });
    expect(call).toBe(2);
  });

  it("words a refusal with the rules it names", () => {
    expect(
      explainRefusal(
        {
          code: "WV-DECISION-MULTIPLE_MATCHES",
          message: "Two rules matched but the policy allows exactly one.",
          path: "/spec/rules/2/when",
        },
        ["match", "no-match", "match"],
      ),
    ).toEqual({
      message:
        "Two rules matched but the policy allows exactly one (rules 1 and 3).",
      rules: [1, 3],
    });
    expect(
      explainRefusal(
        {
          code: "WV-DECISION-PREDICATE",
          message: "A rule's condition isn't true or false.",
          path: "/spec/rules/2/when",
        },
        ["no-match", "no-match", "error"],
      ),
    ).toEqual({
      message: "Rule 3's condition isn't true or false.",
      rules: [3],
    });
    expect(
      explainRefusal(
        {
          code: "WV-DECISION-INPUT",
          message: "The input doesn't fit the table's input fields.",
          path: "/amount",
        },
        [],
      ),
    ).toEqual({
      message: "The input doesn't fit the table's input fields.",
      rules: [],
    });
  });

  it("shows a refusal from Weave in words", async () => {
    const service = new TryInputService(async () => {
      throw failure(
        422,
        "WV-DECISION-MULTIPLE_MATCHES",
        { path: "/spec/rules/2/when" },
        "Two rules matched but the policy allows exactly one.",
      );
    });
    service.set(table("unique"), { amount: 2000, tier: "gold" }, 1);
    await vi.advanceTimersByTimeAsync(400);
    expect(service.state().checked).toEqual({
      refused: {
        code: "WV-DECISION-MULTIPLE_MATCHES",
        message:
          "Two rules matched but the policy allows exactly one (rules 1 and 3).",
        path: "/spec/rules/2/when",
        rules: [1, 3],
      },
    });
  });

  it("retries once when the host is busy", async () => {
    let call = 0;
    const service = new TryInputService(async () => {
      call++;
      if (call === 1) throw failure(503, "WV-STUDIO-BUSY");
      return {
        output: { approve: true },
        matched_rule_ids: ["big"],
        used_default: false,
      };
    });
    service.set(table(), { amount: 2000 }, 1);
    await vi.advanceTimersByTimeAsync(400);
    expect(call).toBe(1);
    await vi.advanceTimersByTimeAsync(400);
    expect(call).toBe(2);
    expect(service.state().checked).toMatchObject({
      answer: { matched_rule_ids: ["big"] },
    });
  });

  it("says when the host can't be reached and keeps the live answer", async () => {
    const service = new TryInputService(async () => {
      throw new TypeError("Failed to fetch");
    });
    service.set(table(), { amount: 2000 }, 1);
    await vi.advanceTimersByTimeAsync(400);
    expect(service.state().checked).toBe("offline-host");
    expect(service.state().live).toMatchObject({ ok: true, matched: ["big"] });
  });

  it("logs, with codes only, when Studio and Weave disagree", async () => {
    const warn = vi.fn();
    const service = new TryInputService(
      async () => {
        throw failure(422, "WV-DECISION-OUTPUT", { path: "/output" });
      },
      { warn },
    );
    service.set(table(), { amount: 2000 }, 1);
    await vi.advanceTimersByTimeAsync(400);
    expect(warn).toHaveBeenCalledWith(
      "Studio and Weave answered this input differently",
      { code: "WV-DECISION-OUTPUT" },
    );
  });
});
