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
// A simulated run played back on the canvas, and what is announced at its end.
import { describe, expect, it } from "vitest";
import type { ExecuteResponse } from "../src/app/editor/state/execution";
import {
  finalAnnouncement,
  finalRun,
  playbackFrames,
} from "../src/app/editor/canvas/trace-playback";
import { RESPONSE } from "./execution-fixtures";

describe("playback", () => {
  it("lights each step in turn, then shows how the run ended", () => {
    const frames = playbackFrames(RESPONSE, false);
    expect(frames.map((f) => [f.current, f.done])).toEqual([
      [["check"], []],
      [["send"], ["check"]],
      [["gone"], ["check", "send"]],
      [[], ["check", "send", "gone"]],
    ]);
    expect(frames.at(-1)?.status).toBe("succeeded");
    expect(playbackFrames(RESPONSE, true)).toEqual([finalRun(RESPONSE)]);
  });
  it("marks the step that blocked or failed, and says so", () => {
    const blocked: ExecuteResponse = {
      ...RESPONSE,
      status: "blocked",
      trace: [RESPONSE.trace[0]],
      blocked: {
        reason: "missing_mock",
        nodeId: "send",
        instanceKey: "",
        message: "Step send needs test data.",
      },
    };
    expect(finalRun(blocked)).toMatchObject({
      status: "waiting",
      current: ["send"],
      done: ["check"],
    });
    expect(finalAnnouncement(blocked)).toBe("Step send needs test data.");
    expect(finalAnnouncement(RESPONSE)).toBe("Executed 3 steps.");
    expect(finalAnnouncement({ ...RESPONSE, status: "failed" })).toBe(
      "The test run failed.",
    );
  });
});
