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
import { expect, it, vi } from "vitest";
import { uploadFile, downloadFile } from "../src/app/forms/core/file-transfer";
import {
  type FileAccess,
  MAX_FILE_BYTES,
  FILE_CHUNK_BYTES,
  type FileReference,
} from "../src/app/forms/core/file-reference";

it("uploads bounded chunks and emits only a verified ready reference", async () => {
  let metadata: any;
  const request = vi.fn(async (path: string, method?: string, body?: any) => {
    if (path.endsWith("/files")) {
      metadata = { ...body, kind: "weave/file", id: "one" };
      return {
        file: metadata,
        state: "uploading",
        received_chunks: [],
        chunk_bytes: FILE_CHUNK_BYTES,
      };
    }
    if (path.endsWith("/finish")) return { file: metadata, state: "ready" };
    return {};
  });
  const access = {
    api: { environment: "/studio/api/env", request },
    canManage: true,
    active: () => true,
  } as unknown as FileAccess;
  const file = new File([new Uint8Array(FILE_CHUNK_BYTES + 3)], "invoice.bin");
  const value = await uploadFile(access, file, () => {});
  expect(value).toEqual(metadata);
  expect(request.mock.calls.map((call) => call[0])).toEqual([
    "/studio/api/env/files",
    "/studio/api/env/files/one/chunks",
    "/studio/api/env/files/one/chunks",
    "/studio/api/env/files/one/finish",
  ]);
  expect(atob(request.mock.calls[1][2].contentBase64).length).toBe(
    FILE_CHUNK_BYTES,
  );
  expect(atob(request.mock.calls[2][2].contentBase64).length).toBe(3);
  expect(JSON.stringify(value)).not.toContain("contentBase64");
});
it("rejects oversized files before reading bytes or making a request", async () => {
  const request = vi.fn();
  const access = {
    api: { request },
    canManage: true,
    active: () => true,
  } as unknown as FileAccess;
  await expect(
    uploadFile(access, { size: MAX_FILE_BYTES + 1 } as File, () => {}),
  ).rejects.toThrow("25 MiB");
  expect(request).not.toHaveBeenCalled();
});
it("stops after workspace changes and never publishes a partial reference", async () => {
  let current = true;
  const request = vi.fn(async () => {
    current = false;
    return {
      file: { id: "one" },
      chunk_bytes: FILE_CHUNK_BYTES,
      received_chunks: [],
    };
  });
  const access = {
    api: { environment: "/studio/api/env", request },
    canManage: true,
    active: () => current,
  } as unknown as FileAccess;
  await expect(
    uploadFile(access, new File(["data"], "invoice.txt"), () => {}),
  ).rejects.toThrow("workspace or form changed");
  expect(request).toHaveBeenCalledTimes(1);
});

it("human task uploads carry the current revision through the narrow endpoints", async () => {
  let file: any;
  const request = vi.fn(async (path: string, _method?: string, body?: any) => {
    if (path.endsWith("/create")) {
      file = { ...body.file, id: "owned", kind: "weave/file" };
      return {
        file,
        state: "uploading",
        received_chunks: [],
        chunk_bytes: FILE_CHUNK_BYTES,
      };
    }
    return { file, state: "ready" };
  });
  const access = {
    api: { environment: "/studio/api/env", request },
    task: { id: "task-1", revision: 7 },
    canManage: true,
    active: () => true,
  } as unknown as FileAccess;
  await uploadFile(access, new File(["proof"], "receipt.txt"), () => {});
  expect(request.mock.calls.map((call) => call[0])).toEqual([
    "/studio/api/env/human-tasks/task-1/files/create",
    "/studio/api/env/human-tasks/task-1/files/chunk",
    "/studio/api/env/human-tasks/task-1/files/finish",
  ]);
  expect(
    request.mock.calls.every((call) => call[2].expected_revision === 7),
  ).toBe(true);
  expect(request.mock.calls[1][2]).toMatchObject({
    file_id: "owned",
    chunk: { index: 0 },
  });
});

it("authorizes empty task downloads through task read before producing a file", async () => {
  const file = {
    kind: "weave/file",
    id: "one",
    filename: "empty.txt",
    contentType: "text/plain",
    sizeBytes: 0,
    sha256: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  } as FileReference;
  const request = vi.fn(async () => ({ file, state: "ready" }));
  const access = {
    api: { environment: "/studio/api/env", request },
    task: { id: "task-1", revision: 7 },
    canRead: true,
    active: () => true,
  } as unknown as FileAccess;
  const result = await downloadFile(access, file);
  expect(request).toHaveBeenCalledWith(
    "/studio/api/env/human-tasks/task-1/files/read",
    "POST",
    { expected_revision: 7, file_id: "one" },
  );
  expect(result.blob.size).toBe(0);
});
it("refuses corrupted downloaded bytes", async () => {
  const file = {
    kind: "weave/file",
    id: "one",
    filename: "receipt.txt",
    contentType: "text/plain",
    sizeBytes: 1,
    sha256: "0000000000000000000000000000000000000000000000000000000000000000",
  } as FileReference;
  const request = vi.fn(async (path: string) =>
    path.endsWith("/download")
      ? { index: 0, contentBase64: "YQ==" }
      : { file, state: "ready" },
  );
  const access = {
    api: { environment: "/studio/api/env", request },
    canRead: true,
    active: () => true,
  } as unknown as FileAccess;
  await expect(downloadFile(access, file)).rejects.toThrow(
    "could not be verified",
  );
});
