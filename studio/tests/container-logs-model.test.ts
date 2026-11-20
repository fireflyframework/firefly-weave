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
import { describe, expect, it, vi } from "vitest";
import type {
  LogRequestView,
  LogRunner,
  LogTarget,
  MemberLog,
} from "../src/app/operate/logs/container-logs-api";
import {
  COPY_FAILED,
  RESTART_NOTE,
  UNREAD_NOTE,
  copiedNote,
  defaultMembers,
  delay,
  downloadName,
  failureNote,
  followWaitingNote,
  followingNote,
  lineCount,
  lineTime,
  memberBoxes,
  memberCount,
  memberText,
  missingNote,
  runnerStatus,
  snapshotNote,
  tailProblem,
} from "../src/app/operate/logs/container-logs-model";

const NOW = Date.parse("2026-11-19T10:54:10Z");
const target: LogTarget = {
  id: "t1",
  name: "local-docker",
  capabilities: ["observe", "read_logs"],
  disabled: false,
};
const runner = (facts: Partial<LogRunner> = {}): LogRunner => ({
  targetId: "t1",
  capabilities: ["observe", "read_logs"],
  lastSeen: "2026-11-19T10:54:05Z",
  expiresAt: "2026-11-19T10:55:35Z",
  revoked: false,
  ...facts,
});

describe("whether a target's runner can fetch its logs", () => {
  it("is ready while a runner that reads logs is in touch", () => {
    expect(runnerStatus(target, [runner()], NOW)).toEqual({ kind: "online" });
  });

  it("says how long the runner has been out of touch", () => {
    const quiet = runner({
      lastSeen: "2026-11-19T10:52:01Z",
      expiresAt: "2026-11-19T10:53:31Z",
    });
    expect(
      runnerStatus(target, [quiet, runner({ revoked: true })], NOW),
    ).toEqual({
      kind: "offline",
      text: "The runner for local-docker hasn't been in touch for 2 min, so Weave can't fetch its logs.",
    });
    expect(runnerStatus(target, [], NOW)).toEqual({
      kind: "offline",
      text: "No runner has registered for local-docker, so Weave can't fetch its logs.",
    });
  });

  it("says when the target or its runner doesn't read logs", () => {
    const unsupported = {
      kind: "unsupported",
      text: "This target's runner can't fetch logs.",
    };
    const older = { ...target, capabilities: ["observe", "scale_workers"] };
    expect(runnerStatus(older, [runner()], NOW)).toEqual(unsupported);
    expect(
      runnerStatus(target, [runner({ capabilities: ["observe"] })], NOW),
    ).toEqual(unsupported);
  });
});

describe("what a fetch asks for", () => {
  it("takes the first five members, and up to 1,000 lines", () => {
    const members = ["a", "b", "c", "d", "e", "f"].map((n) => `worker-${n}`);
    expect(defaultMembers({ name: "weave-worker", members })).toEqual(
      members.slice(0, 5),
    );
    expect(tailProblem("200")).toBe("");
    expect(tailProblem("1")).toBe("");
    expect(tailProblem("1000")).toBe("");
    for (const text of ["0", "1001", "", "12.5", "ten", "-3"])
      expect(tailProblem(text)).toBe(
        "Enter a number of lines from 1 to 1,000.",
      );
  });
});

describe("what a snapshot shows of its members", () => {
  const log = (member: string, restarts = 0): MemberLog => ({
    member,
    lines: [],
    restarts,
    previousLines: 0,
  });
  const snapshot = (requestedMembers: string[], members: MemberLog[]) =>
    ({ requestedMembers, members }) as LogRequestView;

  it("draws every chosen member, and says which ones couldn't be read", () => {
    const boxes = memberBoxes(
      snapshot(["w-2", "w-1", "w-3"], [log("w-1"), log("w-2")]),
    );
    expect(boxes.map((box) => [box.member, box.log?.member ?? null])).toEqual([
      ["w-2", "w-2"],
      ["w-1", "w-1"],
      ["w-3", null],
    ]);
    // With none chosen, the runner read every running member: each one it sent is drawn.
    expect(
      memberBoxes(snapshot([], [log("w-1"), log("w-2")])).map(
        (box) => box.member,
      ),
    ).toEqual(["w-1", "w-2"]);
    expect(UNREAD_NOTE).toBe(
      "Weave couldn't read this member's logs. It may have stopped or been removed since the list was read.",
    );
  });

  it("says why lines may be missing, naming an unread member only when none was drawn", () => {
    expect(missingNote(true)).toBe(
      "Some lines are missing: long lines were cut, or older lines were left out to keep the snapshot within 256 KiB.",
    );
    expect(missingNote(false)).toBe(
      "Some lines are missing: long lines were cut, older lines were left out to keep the snapshot within 256 KiB, or a member couldn't be read.",
    );
  });

  it("counts lines and restarts, and marks where the previous container's lines end", () => {
    expect([memberCount(4, 0), memberCount(4, 1), memberCount(1, 3)]).toEqual([
      "4 lines",
      "4 lines · restarted once",
      "1 line · restarted 3 times",
    ]);
    expect(RESTART_NOTE).toBe(
      "Restarted. Lines above are from the previous container.",
    );
  });

  it("while following, says when the snapshot was updated, and what Follow waits for", () => {
    expect(followingNote("2026-11-19T10:54:15Z", "UTC")).toBe(
      "Following: updated 10:54:15. Snapshots expire after 1 hour. Secrets are redacted before they leave the runner.",
    );
    expect(followWaitingNote("local-docker")).toBe(
      "Follow is waiting for the runner of local-docker. It fetches again once the runner is back in touch.",
    );
    expect([copiedNote(4), copiedNote(1), COPY_FAILED]).toEqual([
      "Copied 4 lines",
      "Copied 1 line",
      "Couldn't copy. Select the lines and copy them.",
    ]);
  });
});

describe("a snapshot on screen", () => {
  const member: MemberLog = {
    member: "weave-worker-7c9f-x2k4",
    restarts: 0,
    previousLines: 0,
    lines: [
      {
        at: "2026-11-19T10:41:52.123Z",
        stream: "stdout",
        text: "Worker ready",
      },
      {
        at: "2026-11-19T10:42:01Z",
        stream: "stderr",
        text: "WARN <b>relay</b> answered 550",
      },
    ],
  };

  it("says when it was read, how long it lasts and what it hides", () => {
    expect(snapshotNote("2026-11-19T10:54:05Z", "UTC")).toBe(
      "Snapshot from 10:54:05 · Snapshots expire after 1 hour. Secrets are redacted before they leave the runner.",
    );
    expect(lineTime("2026-11-19T10:41:52.123Z", "UTC")).toBe("10:41:52");
    expect([lineCount(1), lineCount(4), lineCount(0)]).toEqual([
      "1 line",
      "4 lines",
      "0 lines",
    ]);
  });

  it("copies and downloads as plain text, one line each, with a fixed file name", () => {
    expect(memberText(member)).toBe(
      "2026-11-19T10:41:52.123Z stdout Worker ready\n" +
        "2026-11-19T10:42:01.000Z stderr WARN <b>relay</b> answered 550\n",
    );
    expect(
      downloadName(
        "local-docker",
        "weave-worker",
        "weave-worker-7c9f-x2k4",
        "2026-11-19T10:54:05.900Z",
      ),
    ).toBe(
      "local-docker-weave-worker-weave-worker-7c9f-x2k4-20261119T105405Z.log",
    );
    // Names come from the platform: anything but a file name's characters becomes a dash.
    expect(downloadName("a/b", "c d", "../e", "2026-11-19T10:54:05Z")).toBe(
      "a-b-c-d-..-e-20261119T105405Z.log",
    );
  });

  it("explains each reason a snapshot has no lines", () => {
    expect(failureNote("no_answer", "local-docker", "weave-worker")).toBe(
      "The runner of local-docker didn't answer within 2 minutes. Try again, or check its runner in Operate › Clusters.",
    );
    expect(
      failureNote("unknown_component", "local-docker", "weave-worker"),
    ).toBe("The runner of local-docker doesn't read the logs of weave-worker.");
    expect(failureNote("no_members", "local-docker", "weave-worker")).toBe(
      "None of the members you chose runs in weave-worker anymore. Choose others, or fetch all of them.",
    );
    expect(failureNote("unreadable", "local-docker", "weave-worker")).toBe(
      "The runner of local-docker couldn't read these logs. Try again in a moment.",
    );
  });
});

describe("waiting for a snapshot", () => {
  it("ends at once when cancelled, so nothing awaiting it hangs", async () => {
    vi.useFakeTimers();
    try {
      const waits = [delay(1_000), delay(1_000)];
      const ended = [false, false];
      waits.forEach((wait, index) =>
        wait.done.then(() => (ended[index] = true)),
      );
      waits[0].cancel();
      await vi.advanceTimersByTimeAsync(0);
      expect(ended).toEqual([true, false]);
      // The cancelled wait left no timer behind; the other ends on time.
      expect(vi.getTimerCount()).toBe(1);
      await vi.advanceTimersByTimeAsync(999);
      expect(ended[1]).toBe(false);
      await vi.advanceTimersByTimeAsync(1);
      expect(ended[1]).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });
});
