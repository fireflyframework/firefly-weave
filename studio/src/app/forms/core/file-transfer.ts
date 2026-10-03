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
import {
  FILE_CHUNK_BYTES,
  MAX_FILE_BYTES,
  type FileAccess,
  type FileReference,
  type FileUpload,
} from "./file-reference";

const hash = async (bytes: Uint8Array<ArrayBuffer>) =>
  [...new Uint8Array(await crypto.subtle.digest("SHA-256", bytes))]
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("");
const base64 = (bytes: Uint8Array) => {
  let binary = "";
  for (let start = 0; start < bytes.length; start += 8192)
    binary += String.fromCharCode(...bytes.subarray(start, start + 8192));
  return btoa(binary);
};
const check = (access: FileAccess, alive: () => boolean) => {
  if (!access.active() || !alive())
    throw Error(
      "The file transfer stopped because this workspace or form changed.",
    );
};
export async function uploadFile(
  access: FileAccess,
  file: File,
  progress: (percent: number) => void,
  alive = () => true,
): Promise<FileReference> {
  if (!access.canManage)
    throw Error("Uploading files requires file management access.");
  if (file.size > MAX_FILE_BYTES)
    throw Error("Choose a file of 25 MiB or smaller.");
  check(access, alive);
  const path = access.task
    ? `${access.api.environment}/human-tasks/${encodeURIComponent(access.task.id)}/files`
    : `${access.api.environment}/files`;
  const bytes = new Uint8Array(await file.arrayBuffer());
  const sha256 = await hash(bytes);
  check(access, alive);
  const metadata = {
    filename: file.name,
    contentType: file.type || "application/octet-stream",
    sizeBytes: file.size,
    sha256,
  };
  const upload = await access.api.request<FileUpload>(
    access.task ? `${path}/create` : path,
    "POST",
    access.task
      ? { expected_revision: access.task.revision, file: metadata }
      : metadata,
    {
      "Idempotency-Key": crypto.randomUUID(),
    },
  );
  check(access, alive);
  if (upload.chunk_bytes !== FILE_CHUNK_BYTES)
    throw Error("The server returned an unsupported file chunk size.");
  const received = new Set(upload.received_chunks);
  for (
    let offset = 0, index = 0;
    offset < bytes.length;
    offset += FILE_CHUNK_BYTES, index++
  ) {
    check(access, alive);
    const chunk = {
      index,
      contentBase64: base64(bytes.subarray(offset, offset + FILE_CHUNK_BYTES)),
    };
    if (!received.has(index))
      await access.api.request(
        access.task
          ? `${path}/chunk`
          : `${path}/${encodeURIComponent(upload.file.id)}/chunks`,
        "POST",
        access.task
          ? {
              expected_revision: access.task.revision,
              file_id: upload.file.id,
              chunk,
            }
          : chunk,
      );
    check(access, alive);
    progress(
      Math.round(
        (Math.min(offset + FILE_CHUNK_BYTES, bytes.length) / bytes.length) *
          100,
      ),
    );
  }
  check(access, alive);
  const finished = await access.api.request<FileUpload>(
    access.task
      ? `${path}/finish`
      : `${path}/${encodeURIComponent(upload.file.id)}/finish`,
    "POST",
    access.task
      ? { expected_revision: access.task.revision, file_id: upload.file.id }
      : {},
  );
  check(access, alive);
  if (
    finished.state !== "ready" ||
    finished.file.sha256 !== sha256 ||
    finished.file.sizeBytes !== file.size
  )
    throw Error("The uploaded file could not be verified.");
  progress(100);
  return finished.file;
}
export async function downloadFile(
  access: FileAccess,
  file: FileReference,
  alive = () => true,
): Promise<{ file: FileReference; blob: Blob }> {
  if (!access.canRead)
    throw Error("Downloading files requires file read access.");
  check(access, alive);
  const path = access.task
    ? `${access.api.environment}/human-tasks/${encodeURIComponent(access.task.id)}/files`
    : `${access.api.environment}/files/${encodeURIComponent(file.id)}`;
  const record = access.task
    ? await access.api.request<FileUpload>(`${path}/read`, "POST", {
        expected_revision: access.task.revision,
        file_id: file.id,
      })
    : await access.api.request<FileUpload>(path);
  check(access, alive);
  if (
    record.state !== "ready" ||
    record.file.sizeBytes > MAX_FILE_BYTES ||
    record.file.sizeBytes < 0
  )
    throw Error("This file is not available for download.");
  const bytes = new Uint8Array(record.file.sizeBytes);
  for (
    let offset = 0, index = 0;
    offset < bytes.length;
    offset += FILE_CHUNK_BYTES, index++
  ) {
    check(access, alive);
    const chunk = await access.api.request<{
      index: number;
      contentBase64: string;
    }>(
      `${path}/download`,
      "POST",
      access.task
        ? {
            expected_revision: access.task.revision,
            file_id: file.id,
            chunk: { index },
          }
        : { index },
    );
    check(access, alive);
    const data = Uint8Array.from(atob(chunk.contentBase64), (char) =>
      char.charCodeAt(0),
    );
    if (
      chunk.index !== index ||
      data.length !== Math.min(FILE_CHUNK_BYTES, bytes.length - offset)
    )
      throw Error("The downloaded file is incomplete.");
    bytes.set(data, offset);
  }
  if ((await hash(bytes)) !== record.file.sha256)
    throw Error("The downloaded file could not be verified.");
  check(access, alive);
  return {
    file: record.file,
    blob: new Blob([bytes], { type: record.file.contentType }),
  };
}
