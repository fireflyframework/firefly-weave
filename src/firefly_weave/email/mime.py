# Copyright 2026 Firefly Software Foundation.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Strict bounded MIME normalization and outbound header construction."""

import base64
import re
from email import policy
from email.message import EmailMessage, MIMEPart
from email.parser import BytesParser
from email.utils import getaddresses

from firefly_weave.contracts.email import AttachmentMetadata, EmailSendRequest, NormalizedEmail, address

ID = re.compile(r"<[A-Za-z0-9.!#$%&\'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+>")


def message_ids(value: str) -> tuple[str, ...]:
    if len(value) > 8192 or "\r" in value or "\n" in value:
        raise ValueError("Invalid threading header")
    matches = tuple(ID.findall(value))
    if len(matches) > 50 or ID.sub("", value).strip():
        raise ValueError("Invalid threading header")
    return matches


def parse_message(raw: bytes, *, max_bytes: int = 4 * 1048576) -> NormalizedEmail:
    if len(raw) > max_bytes or b"\r\n\r\n" not in raw[:65536] and b"\n\n" not in raw[:65536]:
        raise ValueError("Message size or header limit exceeded")
    message = BytesParser(policy=policy.default).parsebytes(raw)
    if message.defects:
        raise ValueError("Malformed MIME")
    for name in ("Message-ID", "In-Reply-To", "References", "From", "Reply-To", "Subject"):
        if len(message.get_all(name, [])) > 1:
            raise ValueError("Duplicate header")

    def ids(name: str) -> tuple[str, ...]:
        return message_ids(str(message.get(name, "")))

    mid, parent, refs = ids("Message-ID"), ids("In-Reply-To"), ids("References")
    if len(mid) > 1 or len(parent) > 1 or parent and refs and parent[0] != refs[-1]:
        raise ValueError("Conflicting threading headers")

    def mailboxes(name: str) -> tuple[str, ...]:
        result = tuple(address(v) for _, v in getaddresses([str(v) for v in message.get_all(name, [])]))
        if len(result) > 50:
            raise ValueError("Too many recipients")
        return result

    sender, reply = mailboxes("From"), mailboxes("Reply-To")
    if len(sender) != 1 or len(reply) > 1:
        raise ValueError("Invalid sender")
    parts, texts, attachments = 0, [], []

    def visit(part: MIMEPart, depth: int) -> None:
        nonlocal parts
        parts += 1
        if depth > 8 or parts > 50 or part.defects:
            raise ValueError("MIME complexity exceeded")
        if part.is_multipart():
            for child in part.iter_parts():
                visit(child, depth + 1)
        elif part.get_content_disposition() == "attachment" or part.get_filename():
            attachments.append(
                AttachmentMetadata(
                    filename=(part.get_filename() or "")[:200] or None,
                    content_type=part.get_content_type(),
                    size=len(part.get_payload(decode=True) or b""),
                )
            )
        elif part.get_content_type() == "text/plain":
            payload = part.get_payload(decode=True)
            if not isinstance(payload, bytes):
                raise ValueError("Malformed MIME body")
            data = payload
            texts.append(data.decode(part.get_content_charset() or "utf-8", errors="replace"))

    visit(message, 0)
    text = "\n".join(texts)
    if len(text.encode()) > 1048576:
        raise ValueError("Body too large")
    return NormalizedEmail(
        sender=sender[0],
        to=mailboxes("To"),
        cc=mailboxes("Cc"),
        reply_to=reply[0] if reply else None,
        subject=str(message.get("Subject", ""))[:998],
        text=text,
        message_id=mid[0] if mid else None,
        in_reply_to=parent[0] if parent else None,
        references=refs,
        attachments=tuple(attachments),
        automated=str(message.get("Return-Path", "")) == "<>"
        or str(message.get("Auto-Submitted", "no")).lower() != "no"
        or message.get_content_type() == "multipart/report",
    )


def reply_headers(parent: NormalizedEmail) -> tuple[str, tuple[str, ...]]:
    if parent.message_id is None:
        raise ValueError("Parent has no valid Message-ID")
    references = tuple(dict.fromkeys((*parent.references, parent.message_id)))[-50:]
    return parent.message_id, references


def build_message(
    request: EmailSendRequest,
    sender: str,
    message_id: str,
    *,
    parent: NormalizedEmail | None = None,
    automated: bool = False,
) -> bytes:
    address(sender)
    if message_ids(message_id) != (message_id,):
        raise ValueError("Invalid Message-ID")
    message = EmailMessage(policy=policy.SMTP)
    message["From"], message["To"], message["Subject"], message["Message-ID"] = (
        sender,
        ", ".join(request.to),
        request.subject,
        message_id,
    )
    if automated:
        message["Auto-Submitted"] = "auto-replied" if parent else "auto-generated"
    if request.cc:
        message["Cc"] = ", ".join(request.cc)
    if parent:
        in_reply_to, references = reply_headers(parent)
        message["In-Reply-To"], message["References"] = in_reply_to, " ".join(references)
    message.set_content(request.text)
    if request.html is not None:
        message.add_alternative(request.html, subtype="html")
    for attachment in request.attachments:
        main, sub = attachment.content_type.split("/")
        message.add_attachment(
            base64.b64decode(attachment.content_base64), maintype=main, subtype=sub, filename=attachment.filename
        )
    raw = message.as_bytes()
    if len(raw) > 4 * 1048576:
        raise ValueError("Message too large")
    return raw
