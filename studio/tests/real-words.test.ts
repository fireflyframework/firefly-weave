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
// What step details say when a real run of a step ends without success, from
// the error the platform reports: `WV-STEP-<code>` and its own generic message.
import { describe, expect, it } from "vitest";
import { realEndWords } from "../src/app/editor/ndv/real-words";

const failed = (code: string, message = `The step failed: ${code}.`) => ({
  status: "failed" as const,
  error: { code: `WV-STEP-${code}`, message },
});

describe("how a real run of a step ended", () => {
  it("names the step's own code once, without the platform's prefix", () => {
    expect(
      realEndWords("send-invoice", "local", failed("SMTP_REJECTED"), 60),
    ).toBe("send-invoice failed in local: SMTP_REJECTED.");
  });

  it("adds a message only when it says more than the code", () => {
    expect(
      realEndWords(
        "send-invoice",
        "local",
        failed("SMTP_REJECTED", "The mail server refused the recipient."),
        60,
      ),
    ).toBe(
      "send-invoice failed in local: SMTP_REJECTED. The mail server refused the recipient.",
    );
  });

  it("an email whose delivery the server didn't confirm is never resent", () => {
    expect(
      realEndWords(
        "send-invoice",
        "local",
        failed("EMAIL_ACCEPTANCE_UNKNOWN"),
        60,
      ),
    ).toBe(
      "The mail server's answer was unclear. Weave won't resend it; check the mailbox.",
    );
  });

  it("says when it timed out or was canceled", () => {
    expect(
      realEndWords(
        "send-invoice",
        "local",
        { status: "timed_out", error: null },
        120,
      ),
    ).toBe("send-invoice didn't finish in local within 120 s.");
    expect(
      realEndWords(
        "send-invoice",
        "local",
        { status: "cancelled", error: null },
        120,
      ),
    ).toBe("Canceled.");
    expect(
      realEndWords(
        "send-invoice",
        "local",
        { status: "succeeded", error: null },
        120,
      ),
    ).toBeNull();
  });
});
