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
  LogRunner,
  LogTarget,
  MemberLog,
} from "../src/app/operate/logs/container-logs-api";
import {
  defaultMembers,
  delay,
  downloadName,
  failureNote,
  lineCount,
  lineTime,
  memberText,
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

describe("a snapshot on screen", () => {
  const member: MemberLog = {
    member: "weave-worker-7c9f-x2k4",
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
