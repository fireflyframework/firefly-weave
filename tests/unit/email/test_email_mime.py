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
"""Owned local mail fixtures verify validation and durable boundaries."""

import base64
from email import message_from_bytes, policy
from uuid import UUID

import pytest

from firefly_weave.contracts.email import EmailAttachment, EmailSendRequest
from firefly_weave.email.mime import build_message, parse_message, reply_headers


def test_header_injection():
    with pytest.raises(ValueError):
        EmailSendRequest(
            request_id=UUID("00000000-0000-0000-0000-000000000001"),
            connection_revision_id=UUID("00000000-0000-0000-0000-000000000002"),
            to=("a@example.com",),
            subject="hi\r\nBcc: victim@example.com",
            text="hi",
        )


def test_roundtrip_and_reply():
    request = EmailSendRequest(
        request_id=UUID("00000000-0000-0000-0000-000000000001"),
        connection_revision_id=UUID("00000000-0000-0000-0000-000000000002"),
        to=("a@example.com",),
        subject="Hello",
        text="hi",
    )
    parsed = parse_message(build_message(request, "sender@example.com", "<one@example.com>"))
    assert parsed.message_id == "<one@example.com>"
    assert parsed.text.strip() == "hi"
    assert reply_headers(parsed) == ("<one@example.com>", ("<one@example.com>",))


def test_conflicting_parent_rejected():
    with pytest.raises(ValueError):
        parse_message(
            b"From: a@example.com\r\nTo: b@example.com\r\n"
            b"In-Reply-To: <a@example.com>\r\nReferences: <b@example.com>\r\n\r\nx"
        )


def test_duplicate_id_and_mime_size():
    with pytest.raises(ValueError):
        parse_message(b"Message-ID: <a@example.com>\r\nMessage-ID: <b@example.com>\r\n\r\nx")
    with pytest.raises(ValueError):
        parse_message(b"x" * 100, max_bytes=90)


def test_attachment_metadata_only():
    parsed = parse_message(
        b"From: a@example.com\r\nMIME-Version: 1.0\r\n"
        b"Content-Type: multipart/mixed; boundary=x\r\n\r\n--x\r\n"
        b"Content-Type: text/plain\r\n\r\nhi\r\n--x\r\n"
        b"Content-Disposition: attachment; filename=a.txt\r\n"
        b"Content-Type: text/plain\r\n\r\nsecret blob\r\n--x--\r\n"
    )
    assert parsed.text.strip() == "hi"
    assert parsed.attachments[0].filename == "a.txt"
    assert "secret blob" not in parsed.model_dump_json()


def test_references_do_not_establish_routing_authority():
    from firefly_weave.email.source import correlation_candidates

    mail = parse_message(
        b"From: a@example.com\r\nMessage-ID: <reply@example.com>\r\nReferences: <parent@example.com>\r\n\r\nHi"
    )
    assert correlation_candidates(mail) == ("<parent@example.com>",)
    assert not hasattr(mail, "run_id")


def test_automated_messages_and_replies_are_marked_for_loop_suppression():
    request = EmailSendRequest(
        request_id=UUID("00000000-0000-0000-0000-000000000001"),
        connection_revision_id=UUID("00000000-0000-0000-0000-000000000002"),
        to=("a@example.com",),
        subject="Hello",
        text="hi",
    )
    parent = parse_message(build_message(request, "sender@example.com", "<parent@example.com>"))
    assert not parent.automated
    raw = build_message(request, "sender@example.com", "<one@example.com>", automated=True)
    assert b"Auto-Submitted: auto-generated" in raw
    assert parse_message(raw).automated
    reply = build_message(request, "sender@example.com", "<two@example.com>", parent=parent, automated=True)
    assert b"Auto-Submitted: auto-replied" in reply
    assert parse_message(reply).automated


def request_with(text: str) -> EmailSendRequest:
    return EmailSendRequest(
        request_id=UUID("00000000-0000-0000-0000-000000000001"),
        connection_revision_id=UUID("00000000-0000-0000-0000-000000000002"),
        to=("a@example.com",),
        subject="Fallo de ejecución",
        text=text,
    )


def test_a_non_ascii_text_body_builds_an_ascii_message_that_reads_back_the_same():
    text = "Regla: Fallo de ejecución\nAbre Studio › Operate › Clusters › Approvals.\n"
    raw = build_message(request_with(text), "sender@example.com", "<one@example.com>")
    # The email service stores and sends the message as ASCII.
    raw.decode("ascii")
    assert b"Content-Transfer-Encoding: quoted-printable" in raw
    parsed = parse_message(raw)
    assert (parsed.subject, parsed.text.replace("\r\n", "\n")) == ("Fallo de ejecución", text)


def test_an_ascii_text_body_is_sent_as_it_is():
    raw = build_message(
        request_with("Rule: Run failure rate\nValue: 4.2 %\n"), "sender@example.com", "<one@example.com>"
    )
    assert b"Content-Transfer-Encoding: 7bit" in raw
    assert b"\r\n\r\nRule: Run failure rate\r\nValue: 4.2 %\r\n" in raw


def test_a_non_ascii_html_body_builds_an_ascii_message_that_reads_back_the_same():
    html = "<p>Fallo de ejecución › paso</p>\n"
    request = request_with("Fallo de ejecución\n").model_copy(update={"html": html})
    raw = build_message(request, "sender@example.com", "<one@example.com>")
    raw.decode("ascii")
    parsed = message_from_bytes(raw, policy=policy.default)
    html_part = parsed.get_body(preferencelist=("html",))
    assert html_part is not None and html_part.get_content().replace("\r\n", "\n") == html


def test_an_ascii_html_body_is_sent_as_it_is():
    request = request_with("Rule: Run failure rate\n").model_copy(update={"html": "<p>Rule: Run failure rate</p>\n"})
    raw = build_message(request, "sender@example.com", "<one@example.com>")
    assert b"\r\n\r\n<p>Rule: Run failure rate</p>\r\n" in raw


def test_an_attachment_with_a_non_ascii_name_and_text_builds_an_ascii_message():
    content = base64.b64encode("Fallo de ejecución\n".encode()).decode()
    attachment = EmailAttachment(filename="informe-ñ.txt", content_type="text/plain", content_base64=content)
    request = request_with("hi\n").model_copy(update={"attachments": (attachment,)})
    raw = build_message(request, "sender@example.com", "<one@example.com>")
    raw.decode("ascii")
    (part,) = message_from_bytes(raw, policy=policy.default).iter_attachments()
    assert (part.get_filename(), part.get_payload(decode=True)) == ("informe-ñ.txt", "Fallo de ejecución\n".encode())
