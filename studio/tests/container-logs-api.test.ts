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
import { describe, expect, it } from "vitest";
import { ApiError } from "../src/app/api";
import {
  createLogRequest,
  readComponents,
  readLogRequest,
  readRunners,
  readTargets,
  type ContainerLogsApi,
} from "../src/app/operate/logs/container-logs-api";

const view = {
  id: "r1",
  target_id: "t1",
  component: "weave-worker",
  requested_members: ["weave-worker-7c9f-x2k4"],
  since_seconds: 900,
  tail_lines: 200,
  state: "ready",
  created_at: "2026-11-19T10:54:00Z",
  expires_at: "2026-11-19T11:54:00Z",
  read_at: "2026-11-19T10:54:05Z",
  members: [
    {
      member: "weave-worker-7c9f-x2k4",
      lines: [{ at: "2026-11-19T10:41:52Z", stream: "stderr", text: "WARN" }],
    },
  ],
  truncated: false,
  failure: null,
};

describe("reading a log request", () => {
  it("keeps every field of a snapshot", () => {
    expect(readLogRequest(view)).toEqual({
      id: "r1",
      targetId: "t1",
      component: "weave-worker",
      requestedMembers: ["weave-worker-7c9f-x2k4"],
      sinceSeconds: 900,
      tailLines: 200,
      state: "ready",
      createdAt: "2026-11-19T10:54:00Z",
      expiresAt: "2026-11-19T11:54:00Z",
      readAt: "2026-11-19T10:54:05Z",
      members: view.members,
      truncated: false,
      failure: null,
    });
    const pending = readLogRequest({
      ...view,
      state: "pending",
      read_at: null,
      members: [],
    });
    expect([pending.state, pending.readAt, pending.members]).toEqual([
      "pending",
      null,
      [],
    ]);
  });

  it("refuses an answer that isn't a log request, whole or in one line", () => {
    for (const broken of [
      null,
      [],
      { ...view, state: "claimed" },
      { ...view, failure: "lost" },
      { ...view, tail_lines: 1001 },
      {
        ...view,
        members: [
          { member: "w", lines: [{ at: "x", stream: "stdout", text: "" }] },
        ],
      },
      {
        ...view,
        members: [
          {
            member: "w",
            lines: [{ at: view.read_at, stream: "stdin", text: "" }],
          },
        ],
      },
      {
        ...view,
        members: [
          {
            member: "w",
            lines: [{ at: view.read_at, stream: "stdout", text: 3 }],
          },
        ],
      },
    ]) {
      expect(() => readLogRequest(broken)).toThrow(ApiError);
      try {
        readLogRequest(broken);
      } catch (error) {
        expect((error as ApiError).code).toBe("WV-STUDIO-ANSWER");
      }
    }
  });
});

describe("what a fetch can choose from", () => {
  it("reads targets and runners with every field it needs, and leaves out the rest", () => {
    expect(
      readTargets({
        items: [
          {
            id: "t1",
            name: "local-docker",
            capabilities: ["read_logs"],
            disabled: false,
          },
          {
            id: "t2",
            name: "broken",
            capabilities: "read_logs",
            disabled: false,
          },
        ],
      }),
    ).toEqual([
      {
        id: "t1",
        name: "local-docker",
        capabilities: ["read_logs"],
        disabled: false,
      },
    ]);
    expect(
      readRunners({
        items: [
          {
            target_id: "t1",
            capabilities: ["observe"],
            last_seen: "2026-11-19T10:54:05Z",
            expires_at: "2026-11-19T10:55:35Z",
            revoked: false,
          },
          {
            target_id: "t1",
            capabilities: [],
            last_seen: "never",
            expires_at: "",
            revoked: false,
          },
        ],
      }),
    ).toHaveLength(1);
    expect(() => readTargets({ targets: [] })).toThrow(ApiError);
  });

  it("offers the worker components and members of the target's newest complete observation", () => {
    const observation = (
      observed_at: string,
      members: string[],
      complete = true,
    ) => ({
      target_id: "t1",
      observed_at,
      complete,
      resources: [
        {
          name: "weave-worker",
          kind: "worker",
          members: members.map((host) => ({ host })),
        },
        { name: "weave-api", kind: "api" },
      ],
    });
    const page = {
      items: [
        observation("2026-11-19T10:40:00Z", ["old-1"]),
        observation("2026-11-19T10:53:00Z", ["new-1", "new-2"]),
        observation("2026-11-19T10:54:00Z", ["partial"], false),
        {
          ...observation("2026-11-19T10:59:00Z", ["elsewhere"]),
          target_id: "t2",
        },
      ],
    };
    expect(readComponents(page, "t1")).toEqual([
      { name: "weave-worker", members: ["new-1", "new-2"] },
    ]);
    expect(readComponents({ items: [] }, "t1")).toEqual([]);
  });
});

describe("asking for a snapshot", () => {
  it("sends the request with its idempotency key and reads the answer", async () => {
    const sent: unknown[][] = [];
    const api: ContainerLogsApi = {
      environment: "/studio/api/api/v1/tenants/a/projects/b/environments/c",
      async request<T>(...args: unknown[]): Promise<T> {
        sent.push(args);
        return { ...view, state: "pending", read_at: null, members: [] } as T;
      },
    };
    const body = {
      target_id: "t1",
      component: "weave-worker",
      members: [],
      since_seconds: 900,
      tail_lines: 200,
    };
    const created = await createLogRequest(api, body, "key-1");
    expect(created.state).toBe("pending");
    expect(sent).toEqual([
      [
        `${api.environment}/deployment-log-requests`,
        "POST",
        body,
        { "Idempotency-Key": "key-1" },
      ],
    ]);
  });
});
