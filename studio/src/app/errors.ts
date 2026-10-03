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
// Central plain-language mapping for API and transport failures. Banners show
// the explanation; the stable code is offered separately as a support code.

export interface PlainError {
  message: string;
  code: string;
  status: number;
}

const byCode: Record<string, string> = {
  "WV-STUDIO-SESSION":
    "Your Studio session ended. Pair this window again to continue.",
  "WV-STUDIO-CSRF":
    "This Studio window is out of date. Reload the page and try again.",
  "WV-STUDIO-BUSY":
    "Studio is busy with other requests. Wait a moment and try again.",
  "WV-OPERATION-CAPACITY":
    "The platform is busy with other requests. Wait a moment and try again.",
  "WV-REQUEST-CAPACITY":
    "The platform is busy with other requests. Wait a moment and try again.",
  "WV-STUDIO-PAIRING":
    "That pairing code is invalid or has expired. Restart Weave Studio to get a new code.",
  "WV-STUDIO-REQUEST":
    "Studio could not read that request. Reload the page and try again.",
  "WV-STUDIO-SCOPE":
    "That workspace is not available to your account. Choose another workspace in Settings.",
  "WV-STUDIO-IDENTITY":
    "Studio could not confirm your account with the platform. Try again in a moment.",
  "WV-STUDIO-CONNECTION-CHANGED":
    "The platform connection changed while this request was running. Try again.",
  "WV-REVISION":
    "Someone else changed this item. Reload it to see the latest version, then try again.",
  "WV-DENIED":
    "Your account does not have permission for this action in the current workspace. Ask an administrator for access.",
  "WV-CONNECTION":
    "The connection bindings do not match what this workflow needs. Review each connection slot.",
  "WV-STUDIO-LOGIN":
    "This sign-in is no longer active. Start signing in again.",
  "WV-AUTH-REQUIRED":
    "Your sign-in for this platform ended. Sign in again to continue; you can keep working locally.",
  "WV-AUTH-NOT-LINKED":
    "You signed in, but this platform doesn't recognize your account yet. Ask your administrator to link it.",
  "WV-AUTH-STORE":
    "Studio can't use this computer's credential store. Unlock or set up the system keychain, then try again.",
  "WV-PROFILE-NOT-FOUND":
    "That saved platform no longer exists. Refresh Settings and try again.",
};

const byStatus = (status: number): string => {
  if (status === 0)
    return "Studio could not reach its local host. Check that Weave Studio is still running, then try again.";
  if (status === 400 || status === 422)
    return "The platform rejected some values in this request. Review them and try again.";
  if (status === 401) return "Sign in again to continue.";
  if (status === 403)
    return "Your account does not have permission for this action in the current workspace. Ask an administrator for access.";
  if (status === 404)
    return "This item was not found. It may have been removed, or your account cannot see it.";
  if (status === 408)
    return "The request took too long. Check the current state before trying again.";
  if (status === 409)
    return "This item changed since you opened it. Reload it and try again.";
  if (status === 413) return "This request is too large for the platform.";
  if (status === 429)
    return "Too many requests right now. Wait a moment and try again.";
  if (status === 502 || status === 503 || status === 504)
    return "The platform could not be reached. Check your network connection and try again.";
  if (status >= 500)
    return "The platform could not complete this request. Try again; if it keeps failing, contact your administrator.";
  return "The request could not be completed. Try again.";
};

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

/** True when a server-provided sentence is safe and readable as banner text. */
export function readable(text: unknown): text is string {
  return (
    typeof text === "string" &&
    text.length > 0 &&
    text.length <= 240 &&
    !/[{}[\]<>]/.test(text) &&
    !/^HTTP \d+$/.test(text)
  );
}

/** Explains a failed API response from its HTTP status and response body. */
export function describeResponse(status: number, detail: unknown): PlainError {
  const body = record(detail);
  const code = String(body["code"] ?? body["error_code"] ?? "");
  const message =
    byCode[code] ??
    (readable(body["message"])
      ? body["message"]
      : readable(body["detail"])
        ? body["detail"]
        : byStatus(status));
  return { message, code, status };
}

/** Explains any thrown value: API errors, transport failures or plain errors. */
export function describeError(error: unknown): PlainError {
  if (error && typeof error === "object" && "plain" in error) {
    const plain = (error as { plain: unknown }).plain;
    if (plain && typeof plain === "object" && "message" in plain)
      return plain as PlainError;
  }
  if (error instanceof DOMException && error.name === "TimeoutError")
    return {
      message:
        "Studio did not answer in time. Check that Weave Studio is still running, then try again.",
      code: "",
      status: 0,
    };
  if (error instanceof TypeError && /fetch|network/i.test(error.message))
    return { message: byStatus(0), code: "", status: 0 };
  if (error instanceof Error)
    return { message: error.message, code: "", status: 0 };
  return { message: String(error), code: "", status: 0 };
}
