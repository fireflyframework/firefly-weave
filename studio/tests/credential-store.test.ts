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
import {
  CredentialStoreUnavailable,
  credentialAccounts,
  credentialExists,
  deleteCredential,
  parseCmdkey,
  parseKeychainDump,
  parseSecretTool,
  type Run,
} from "./credential-store";

const keychain = [
  'keychain: "/Users/me/Library/Keychains/login.keychain-db"',
  "version: 512",
  'class: "genp"',
  "attributes:",
  '    "acct"<blob>="binding-a"',
  '    "svce"<blob>="firefly-weave"',
  'keychain: "/Users/me/Library/Keychains/login.keychain-db"',
  'class: "genp"',
  "attributes:",
  '    "acct"<blob>="someone"',
  '    "svce"<blob>="another-service"',
].join("\n");
const secretToolOut = [
  "[/org/freedesktop/secrets/collection/login/1]",
  "label = Password for 'binding-a' on 'firefly-weave'",
  "secret = do-not-print",
  "created = 2026-10-07 10:00:00",
  "modified = 2026-10-07 10:00:00",
].join("\n");
const secretToolErr = [
  "schema = org.freedesktop.Secret.Generic",
  "attribute.application = Python keyring library",
  "attribute.service = firefly-weave",
  "attribute.username = binding-a",
].join("\n");
const cmdkey = [
  "",
  "Currently stored credentials:",
  "",
  "    Target: LegacyGeneric:target=firefly-weave",
  "    Type: Generic ",
  "    User: binding-b",
  "    Local machine persistence",
  "",
  "    Target: LegacyGeneric:target=binding-a@firefly-weave",
  "    Type: Generic ",
  "    User: binding-a",
  "",
  "    Target: LegacyGeneric:target=git:https://github.com",
  "    Type: Generic ",
  "    User: someone",
  "",
].join("\r\n");

const ok =
  (stdout: string, stderr = ""): Run =>
  () => ({ status: 0, stdout, stderr });
const failing: Run = () => ({
  status: 1,
  stdout: "",
  stderr: "Cannot autolaunch D-Bus",
});
const missing: Run = () => ({
  status: null,
  stdout: "",
  stderr: "",
  error: new Error("spawnSync secret-tool ENOENT"),
});

describe("credential-store audit", () => {
  it("parses each store's listing", () => {
    expect(parseKeychainDump(keychain, "firefly-weave")).toEqual(
      new Set(["binding-a"]),
    );
    expect(parseSecretTool(`${secretToolOut}\n${secretToolErr}`)).toEqual(
      new Set(["binding-a"]),
    );
    expect(parseCmdkey(cmdkey, "firefly-weave")).toEqual(
      new Map([
        ["binding-b", "firefly-weave"],
        ["binding-a", "binding-a@firefly-weave"],
      ]),
    );
  });
  it("lists accounts through each operating system's own tool", () => {
    const calls: string[][] = [];
    const record =
      (stdout: string, stderr = ""): Run =>
      (command, args) => {
        calls.push([command, ...args]);
        return { status: 0, stdout, stderr };
      };
    expect(
      credentialAccounts("firefly-weave", {
        platform: "darwin",
        run: record(keychain),
      }),
    ).toEqual(new Set(["binding-a"]));
    expect(
      credentialAccounts("firefly-weave", {
        platform: "linux",
        run: record(secretToolOut, secretToolErr),
      }),
    ).toEqual(new Set(["binding-a"]));
    expect(
      credentialAccounts("firefly-weave", {
        platform: "win32",
        run: record(cmdkey),
      }),
    ).toEqual(new Set(["binding-a", "binding-b"]));
    expect(calls).toEqual([
      ["security", "dump-keychain"],
      ["secret-tool", "search", "--all", "service", "firefly-weave"],
      ["cmdkey", "/list"],
    ]);
    expect(
      credentialExists("firefly-weave", "binding-b", {
        platform: "win32",
        run: ok(cmdkey),
      }),
    ).toBe(true);
  });
  it("fails instead of returning an empty audit when the store is unavailable", () => {
    for (const platform of ["darwin", "linux", "win32"] as const)
      expect(() =>
        credentialAccounts("firefly-weave", { platform, run: failing }),
      ).toThrow(CredentialStoreUnavailable);
    expect(() =>
      credentialAccounts("firefly-weave", { platform: "linux", run: missing }),
    ).toThrow(/libsecret-tools/);
    expect(() =>
      credentialAccounts("firefly-weave", { platform: "freebsd", run: ok("") }),
    ).toThrow(/No supported credential store/);
  });
  it("never puts store output in its errors", () => {
    const leaky: Run = () => ({
      status: 2,
      stdout: "secret = do-not-print",
      stderr: "",
    });
    expect(() =>
      credentialAccounts("firefly-weave", { platform: "linux", run: leaky }),
    ).toThrow(/^(?!.*do-not-print)/);
  });
  it("deletes through the repository's keyring library", () => {
    const calls: string[][] = [];
    const run: Run = (command, args) => {
      calls.push([command, ...args]);
      return { status: 0, stdout: "", stderr: "" };
    };
    deleteCredential("firefly-weave", "binding-a", {
      run,
      python: "/repo/python",
    });
    expect(calls).toEqual([
      [
        "/repo/python",
        "-c",
        "import keyring, sys; keyring.delete_password(sys.argv[1], sys.argv[2])",
        "firefly-weave",
        "binding-a",
      ],
    ]);
    expect(() =>
      deleteCredential("firefly-weave", "binding-a", {
        run: () => ({ status: 1, stdout: "", stderr: "" }),
        python: "/x",
      }),
    ).toThrow(/Could not delete/);
  });
});
