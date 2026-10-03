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
import { describe, it, expect } from "vitest";
import { describeError, describeResponse } from "../src/app/errors";
import { ApiError } from "../src/app/api";

describe("plain-language API errors", () => {
  it("explains known codes and keeps the code as a support code", () => {
    const plain = describeResponse(401, {
      status: 401,
      code: "WV-STUDIO-SESSION",
      message: "Studio session required",
    });
    expect(plain.message).toBe(
      "Your Studio session ended. Pair this window again to continue.",
    );
    expect(plain.code).toBe("WV-STUDIO-SESSION");
  });
  it("never sends a Studio user to the command line to sign in again", () => {
    const plain = describeResponse(401, {
      status: 401,
      code: "WV-AUTH-REQUIRED",
      message: "Sign in again with weave auth login using the selected profile",
    });
    expect(plain.message).not.toContain("weave auth login");
    expect(plain.message).toContain("Sign in again");
    expect(plain.code).toBe("WV-AUTH-REQUIRED");
  });
  it("uses a readable server sentence and never raw JSON", () => {
    expect(
      describeResponse(422, { code: "WV-X", message: "Name is too long" })
        .message,
    ).toBe("Name is too long");
    const raw = describeResponse(500, {
      code: "WV-X",
      message: '{"trace":"x"}',
    });
    expect(raw.message).not.toContain("{");
    expect(raw.message).toContain("could not complete this request");
  });
  it("falls back to the HTTP status", () => {
    expect(describeResponse(403, {}).message).toContain("permission");
    expect(describeResponse(404, null).message).toContain("not found");
    expect(describeResponse(503, "busy").message).toContain(
      "could not be reached",
    );
    expect(describeResponse(418, { message: "HTTP 418" }).message).toBe(
      "The request could not be completed. Try again.",
    );
  });
  it("gives ApiError a plain message and code", () => {
    const error = new ApiError(409, { code: "WV-REVISION", revision: 2 });
    expect(error.message).toContain("Someone else changed this item");
    expect(error.message).not.toContain("{");
    expect(error.code).toBe("WV-REVISION");
    expect(describeError(error)).toEqual(error.plain);
  });
  it("explains transport failures and keeps plain errors", () => {
    expect(describeError(new TypeError("Failed to fetch")).message).toContain(
      "could not reach its local host",
    );
    expect(
      describeError(new DOMException("timed out", "TimeoutError")).message,
    ).toContain("did not answer in time");
    expect(describeError(new Error("Place every step first.")).message).toBe(
      "Place every step first.",
    );
  });
});
