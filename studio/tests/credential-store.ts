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
// Per-OS audit of the system credential store for real-platform tests: which
// accounts of a service exist, and removal of the ones a run created. macOS
// reads the login Keychain (security), Linux the Secret Service
// (secret-tool), Windows the Credential Manager (cmdkey). An unavailable
// store is an error, never an empty audit. Store output can hold secrets: it
// is parsed for account names only and never printed.
import { spawnSync } from "node:child_process";
import { python as repositoryPython } from "./python-path";

export interface CommandResult {
  status: number | null;
  stdout: string;
  stderr: string;
  error?: Error;
}
export type Run = (command: string, args: readonly string[]) => CommandResult;

export class CredentialStoreUnavailable extends Error {}

interface Options {
  platform?: NodeJS.Platform;
  run?: Run;
  python?: string;
}

const defaultRun: Run = (command, args) => {
  const result = spawnSync(command, [...args], {
    encoding: "utf8",
    maxBuffer: 256 * 1024 * 1024,
    stdio: ["ignore", "pipe", "pipe"],
  });
  return {
    status: result.status,
    stdout: result.stdout ?? "",
    stderr: result.stderr ?? "",
    error: result.error,
  };
};

/** Accounts of `service` in a `security dump-keychain` listing (attributes only). */
export function parseKeychainDump(dump: string, service: string): Set<string> {
  const accounts = new Set<string>();
  for (const block of dump.split(/^keychain: /m)) {
    if (!block.includes(`"svce"<blob>="${service}"`)) continue;
    const account = block.match(/"acct"<blob>="([^"]*)"/)?.[1];
    if (account) accounts.add(account);
  }
  return accounts;
}

/** Accounts in `secret-tool search --all service …` output (stdout and stderr together). */
export function parseSecretTool(output: string): Set<string> {
  return new Set(
    [...output.matchAll(/^attribute\.username = (.+)$/gm)].map((match) =>
      match[1].trim(),
    ),
  );
}

/** Accounts of `service` and their Credential Manager targets in `cmdkey /list` output. */
export function parseCmdkey(
  output: string,
  service: string,
): Map<string, string> {
  const accounts = new Map<string, string>();
  let target: string | null = null;
  for (const raw of output.split(/\r?\n/)) {
    const line = raw.trim();
    const found = /^Target: (?:LegacyGeneric:target=)?(.+)$/.exec(line);
    if (found) {
      target = found[1];
      continue;
    }
    const user = /^User: (.+)$/.exec(line);
    if (
      user &&
      target &&
      (target === service || target.endsWith(`@${service}`))
    )
      accounts.set(user[1], target);
  }
  return accounts;
}

function unavailable(what: string, result: CommandResult): never {
  const reason = result.error ? result.error.message : `exit ${result.status}`;
  throw new CredentialStoreUnavailable(
    `${what} is not available (${reason}); the credential audit cannot run.`,
  );
}

/** Accounts stored for `service`; throws CredentialStoreUnavailable when the store cannot be read. */
export function credentialAccounts(
  service: string,
  options: Options = {},
): Set<string> {
  const platform = options.platform ?? process.platform;
  const run = options.run ?? defaultRun;
  if (platform === "darwin") {
    const result = run("security", ["dump-keychain"]);
    if (result.error || result.status !== 0)
      unavailable("The macOS login Keychain", result);
    return parseKeychainDump(result.stdout, service);
  }
  if (platform === "linux") {
    const result = run("secret-tool", ["search", "--all", "service", service]);
    if (result.error || result.status !== 0)
      unavailable(
        "The Secret Service (secret-tool from libsecret-tools)",
        result,
      );
    return parseSecretTool(`${result.stdout}\n${result.stderr}`);
  }
  if (platform === "win32") {
    const result = run("cmdkey", ["/list"]);
    if (result.error || result.status !== 0)
      unavailable("The Windows Credential Manager", result);
    return new Set(parseCmdkey(result.stdout, service).keys());
  }
  throw new CredentialStoreUnavailable(
    `No supported credential store on ${platform}.`,
  );
}

/** True when the store still holds this account of `service`. */
export function credentialExists(
  service: string,
  account: string,
  options: Options = {},
): boolean {
  return credentialAccounts(service, options).has(account);
}

/** Deletes one account through the keyring library the Studio host itself uses. */
export function deleteCredential(
  service: string,
  account: string,
  options: Options = {},
): void {
  const run = options.run ?? defaultRun;
  const result = run(options.python ?? repositoryPython, [
    "-c",
    "import keyring, sys; keyring.delete_password(sys.argv[1], sys.argv[2])",
    service,
    account,
  ]);
  if (result.error || result.status !== 0)
    throw new Error(
      `Could not delete the ${service} credential for ${account} (exit ${result.status}).`,
    );
}
