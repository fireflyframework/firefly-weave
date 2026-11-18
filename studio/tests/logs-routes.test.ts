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
// The Logs page's address holds the range, level, workflow, step, code and
// whether test runs show; never the message filter, which has no place in it.
import { describe, expect, it } from "vitest";
import {
  defaultLogsRoute,
  logsPath,
  logsRoute,
} from "../src/app/operate/operate-routes";

describe("the Logs page's address", () => {
  it("reads the defaults from an empty address and each filter from its own", () => {
    expect(logsRoute("")).toEqual(defaultLogsRoute);
    expect(defaultLogsRoute).toEqual({
      range: "1h",
      level: "warning",
      workflow: "",
      step: "",
      code: "",
      test: false,
    });
    expect(
      logsRoute(
        "?range=6h&level=all&workflow=invoice-approval&step=send&code=SMTP_REJECTED&test=1",
      ),
    ).toEqual({
      range: "6h",
      level: "",
      workflow: "invoice-approval",
      step: "send",
      code: "SMTP_REJECTED",
      test: true,
    });
  });
  it("falls back to the default for any value it doesn't know", () => {
    expect(
      logsRoute(
        "?range=30d&level=fatal&workflow=%3Cscript%3E&step=a%20b&code=-x&test=yes",
      ),
    ).toEqual(defaultLogsRoute);
  });
  it("writes only what differs from the defaults, and never a message", () => {
    expect(logsPath(defaultLogsRoute)).toBe("/operate/logs");
    const route = {
      range: "24h" as const,
      level: "error" as const,
      workflow: "invoice-approval",
      step: "send",
      code: "SMTP_REJECTED",
      test: true,
    };
    const path = logsPath(route);
    expect(path).toBe(
      "/operate/logs?range=24h&level=error&workflow=invoice-approval&step=send&code=SMTP_REJECTED&test=1",
    );
    expect(logsRoute(path.slice(path.indexOf("?")))).toEqual(route);
    expect(logsPath({ ...defaultLogsRoute, level: "" })).toBe(
      "/operate/logs?level=all",
    );
  });
});
