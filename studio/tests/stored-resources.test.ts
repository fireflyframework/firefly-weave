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
// What a trigger choice keeps about the platform, per environment: the
// trigger an activation made or moved, and the stops Studio still owes. Both
// come back from a canvas file, so anything malformed counts as nothing.
import { describe, expect, it } from "vitest";
import {
  pendingStopsOf,
  storedId,
  withPendingStops,
} from "../src/app/editor/activation/stored-resources";
import type { TriggerIntent } from "../src/app/editor/state/canvas-sidecar";

const STOP = { triggerId: "t-1", revision: 2, activationId: "a-old" };
const OTHER = { triggerId: "t-2", revision: 1, activationId: "a-older" };
const intent = (fields: Record<string, unknown> = {}): TriggerIntent => ({
  id: "trigger-1b2c3d4e",
  kind: "event",
  ...fields,
});

describe("the trigger stored for an environment", () => {
  it("is the ID kept for that environment only", () => {
    const stored = intent({ resources: { local: "t-1", prod: "t-9" } });
    expect(storedId(stored, "local")).toBe("t-1");
    expect(storedId(stored, "staging")).toBeUndefined();
    expect(storedId(intent(), "local")).toBeUndefined();
    expect(
      storedId(intent({ resources: { local: 7 } }), "local"),
    ).toBeUndefined();
  });
});

describe("the stops Studio owes", () => {
  it("are the ones kept for that environment, without anything malformed", () => {
    const owing = intent({
      pendingStops: {
        local: [
          STOP,
          null,
          "t-3",
          { triggerId: "t-4" },
          { triggerId: "t-5", revision: "2", activationId: "a-old" },
          { triggerId: "t-6", revision: 2, activationId: 4 },
        ],
        prod: [OTHER],
      },
    });
    expect(pendingStopsOf(owing, "local")).toEqual([STOP]);
    expect(pendingStopsOf(owing, "prod")).toEqual([OTHER]);
    for (const pendingStops of [undefined, null, [STOP], { local: STOP }])
      expect(pendingStopsOf(intent({ pendingStops }), "local")).toEqual([]);
  });

  it("are written for one environment and leave the others", () => {
    const owing = intent({ pendingStops: { prod: [OTHER] } });
    expect(withPendingStops(owing, "local", [STOP])).toEqual(
      intent({ pendingStops: { prod: [OTHER], local: [STOP] } }),
    );
  });

  it("leave no trace once none is owed", () => {
    const owing = intent({ pendingStops: { local: [STOP] } });
    expect(withPendingStops(owing, "local", [])).toEqual(intent());
    expect(
      withPendingStops(
        intent({ pendingStops: { local: [STOP], prod: [OTHER] } }),
        "local",
        [],
      ),
    ).toEqual(intent({ pendingStops: { prod: [OTHER] } }));
    const none = intent();
    expect(withPendingStops(none, "local", [])).toBe(none);
  });
});
