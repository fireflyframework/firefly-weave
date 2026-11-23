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
// Studio's words for transaction states, step statuses and codes, read from
// the corpus the platform's own test pins (tests/unit/test_transaction_corpus.py).
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import {
  participantText,
  phaseRowText,
  stateBadge,
  stateWord,
  TRANSACTION_CODE_TEXT,
  wentAhead,
  type ParticipantStatus,
  type TransactionFacts,
  type TransactionState,
  type UndoMode,
} from "../src/app/editor/transactions/transaction-words";

const corpus = JSON.parse(
  readFileSync(
    resolve(import.meta.dirname, "fixtures/transaction-corpus.json"),
    "utf8",
  ),
) as {
  states: Record<
    TransactionState,
    { facts?: Partial<TransactionFacts>; badge: string; tone: string }
  >;
  stateWords: Record<TransactionState, string>;
  statuses: Record<ParticipantStatus, { word: string; tone: string } | null>;
  codes: Record<string, string>;
};
const entries = <K extends string, V>(record: Record<K, V>) =>
  Object.entries(record) as [K, V][];

describe("transaction words", () => {
  it("gives every state its frame badge from the corpus", () => {
    for (const [state, { facts, badge, tone }] of entries(corpus.states))
      expect(stateBadge({ ...facts, state }), state).toEqual({
        text: badge,
        tone,
      });
  });

  it("names every state in one word for Operate's lists and filters", () => {
    for (const [state, word] of entries(corpus.stateWords))
      expect(stateWord(state), state).toBe(word);
  });

  it("gives every step status its tile word from the corpus", () => {
    for (const [status, words] of entries(corpus.statuses))
      for (const mode of ["reserve", "compensate", "irreversible"] as const)
        expect(participantText(status, mode), `${status} ${mode}`).toEqual(
          words && { text: words.word, tone: words.tone },
        );
  });

  it("holds the platform's sentence for every transaction code", () => {
    expect(TRANSACTION_CODE_TEXT).toEqual(corpus.codes);
  });

  it("waits for the named step, or says which kind of step without its name", () => {
    expect(stateBadge({ state: "deciding", pivot: "submit" }).text).toBe(
      "Waiting for submit, which can't be undone",
    );
    expect(stateBadge({ state: "deciding" })).toEqual({
      text: "Waiting for the step that can't be undone",
      tone: "live",
    });
  });

  it("counts the steps undone only while it knows them", () => {
    expect(
      stateBadge({ state: "compensating", undoing: { done: 1, of: 2 } }).text,
    ).toBe("Undoing · 1 of 2");
    expect(stateBadge({ state: "compensating" })).toEqual({
      text: "Undoing",
      tone: "live",
    });
  });

  it("names an undo, release or confirm row, with its try only after the first", () => {
    expect(phaseRowText("undo", "add")).toBe("Undo of add");
    expect(phaseRowText("cancel", "hold")).toBe("Release of hold");
    expect(phaseRowText("confirm", "hold[3]")).toBe("Confirm of hold[3]");
    expect(phaseRowText("undo", "add", { attempt: 3, of: 10 })).toBe(
      "Undo of add (try 3 of 10)",
    );
    expect(phaseRowText("undo", "add", { attempt: 1, of: 10 })).toBe(
      "Undo of add",
    );
  });
});

describe("a step still applied in a transaction left in doubt", () => {
  const done = (
    mode: UndoMode,
    frame?: Parameters<typeof participantText>[2],
  ) => participantText("done", mode, frame);
  const stillApplied = {
    reserve: { text: "Still reserved", tone: "warning" },
    compensate: { text: "Not undone", tone: "warning" },
    irreversible: { text: "Done", tone: "success" },
  } as const;

  it("test_a_step_still_applied_in_a_frame_in_doubt_reads_by_its_undo_mode", () => {
    for (const state of ["in_doubt", "force_ended"] as const) {
      for (const mode of ["reserve", "compensate", "irreversible"] as const)
        expect(done(mode, { state }), `${state} ${mode}`).toEqual(
          stillApplied[mode],
        );
      // The transaction went ahead: what is done stays applied on purpose.
      for (const mode of ["reserve", "compensate", "irreversible"] as const)
        expect(done(mode, { state, wentAhead: true }), mode).toEqual({
          text: "Done",
          tone: "success",
        });
      expect(
        done("reserve", { state, wentAhead: wentAhead([]) }),
        "a frame without participants",
      ).toEqual(stillApplied.reserve);
    }
    for (const frame of [
      { state: "compensated" as const },
      { state: "completed" as const },
      undefined,
    ])
      for (const mode of ["reserve", "compensate"] as const)
        expect(done(mode, frame), `${frame?.state} ${mode}`).toEqual({
          text: "Done",
          tone: "success",
        });
  });

  it("never reads the incident's code, only whether the transaction went ahead", () => {
    expect(
      done("reserve", { state: "in_doubt", code: "WV-TXN-CONFIRM-STUCK" }),
    ).toEqual(stillApplied.reserve);
  });

  it("keeps the word of a step whose undo already says what happened", () => {
    expect(
      participantText("undo_stuck", "compensate", { state: "in_doubt" }),
    ).toEqual({ text: "Undo failed", tone: "danger" });
    expect(
      participantText("cancelled", "reserve", { state: "force_ended" }),
    ).toEqual({ text: "Released", tone: "warning" });
  });
});

describe("whether a transaction went ahead", () => {
  const step = (mode: UndoMode, status: ParticipantStatus) => ({
    mode,
    status,
  });

  it("test_went_ahead_is_read_from_the_participants", () => {
    // The step that can't be undone is done: a step after the point of no return got stuck.
    expect(
      wentAhead([
        step("reserve", "done"),
        step("compensate", "done"),
        step("irreversible", "done"),
        step("compensate", "failed"),
      ]),
    ).toBe(true);
    for (const status of ["confirming", "confirmed", "confirm_stuck"] as const)
      expect(
        wentAhead([step("reserve", status), step("compensate", "done")]),
        status,
      ).toBe(true);
    // An undo stuck before the point of no return.
    expect(
      wentAhead([
        step("reserve", "done"),
        step("compensate", "undo_stuck"),
        step("irreversible", "failed"),
      ]),
    ).toBe(false);
    expect(
      wentAhead([
        step("reserve", "done"),
        step("compensate", "done"),
        step("irreversible", "unknown"),
      ]),
    ).toBe(false);
    expect(wentAhead([])).toBe(false);
  });
});
