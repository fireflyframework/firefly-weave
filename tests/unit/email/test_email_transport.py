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

import asyncio

import pytest

from firefly_weave.contracts.email import MailProfile
from firefly_weave.email.transport import MailPolicy, SMTPTransport


@pytest.mark.parametrize("ambiguous", [False, True])
async def test_smtp_owned_fixture(ambiguous):
    received = []

    async def smtp(reader, writer):
        writer.write(b"220 local\r\n")
        await writer.drain()
        while line := await reader.readline():
            command = line.split(b" ", 1)[0].strip().upper()
            if command == b"EHLO":
                writer.write(b"250 local\r\n")
            elif command == b"MAIL":
                writer.write(b"250 ok\r\n")
            elif command == b"RCPT":
                writer.write(b"250 ok\r\n" if b"a@example.com" in line else b"550 denied\r\n")
            elif command == b"DATA":
                writer.write(b"354 go\r\n")
                await writer.drain()
                payload = []
                while (line := await reader.readline()) != b".\r\n":
                    payload.append(line)
                received.append(b"".join(payload))
                if ambiguous:
                    break
                writer.write(b"250 accepted\r\n")
            await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(smtp, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        profile = MailProfile(
            host="127.0.0.1",
            port=port,
            tls="local_fixture",
            sender="s@example.com",
            allowed_recipients=("a@example.com", "b@example.com"),
            private_networks=("127.0.0.0/8",),
        )
        result = await SMTPTransport(
            MailPolicy(private_networks=("127.0.0.0/8",), allowed_ports=(port,), allow_local_fixture=True)
        ).send(profile, ("a@example.com", "b@example.com"), b"From: s@example.com\r\n\r\nhello\r\n", {})
    assert result.state == ("unknown" if ambiguous else "accepted")
    assert result.accepted_recipients == ("a@example.com",)
    assert result.rejected_recipients == ("b@example.com",)
    assert len(received) == 1


async def test_imap_read_only_peek_and_oversize_metadata():
    from firefly_weave.email.transport import IMAPTransport

    commands = []
    raw = b"From: a@example.com\r\nTo: b@example.com\r\n\r\nhello\r\n"

    async def imap(reader, writer):
        writer.write(b"* OK local\r\n")
        await writer.drain()
        while line := await reader.readline():
            commands.append(line)
            tag, command = line.split(b" ", 1)
            if command.startswith(b"EXAMINE"):
                writer.write(b"* OK [UIDVALIDITY 12] valid\r\n")
            elif command.startswith(b"UID SEARCH"):
                writer.write(b"* SEARCH 1 2\r\n")
            elif command.startswith(b"UID FETCH 1 (RFC822.SIZE)"):
                writer.write(f"* 1 FETCH (RFC822.SIZE {len(raw)})\r\n".encode())
            elif command.startswith(b"UID FETCH 2 (RFC822.SIZE)"):
                writer.write(b"* 2 FETCH (RFC822.SIZE 99999)\r\n")
            elif command.startswith(b"UID FETCH 1 (BODY.PEEK[])"):
                writer.write(f"* 1 FETCH (BODY[] {{{len(raw)}}}\r\n".encode() + raw + b")\r\n")
            else:
                raise AssertionError("Unexpected IMAP command")
            writer.write(tag + b" OK done\r\n")
            await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(imap, "127.0.0.1", 0)
    async with server:
        profile = MailProfile(
            host="127.0.0.1",
            port=server.sockets[0].getsockname()[1],
            tls="local_fixture",
            sender="s@example.com",
            allowed_recipients=("a@example.com",),
            private_networks=("127.0.0.0/8",),
        )
        result = await IMAPTransport(
            MailPolicy(private_networks=("127.0.0.0/8",), allowed_ports=(profile.port,), allow_local_fixture=True)
        ).poll(profile, {}, 0, max_bytes=100)
    assert result.uidvalidity == 12
    assert result.messages == ((1, raw), (2, None))
    assert all(b"SELECT" not in value and b"STORE" not in value for value in commands)
    assert any(b"BODY.PEEK[]" in value for value in commands)


def test_connection_cannot_grant_private_or_plaintext_egress():
    from firefly_weave.connectors.egress import EgressDenied

    profile = MailProfile(
        host="127.0.0.1",
        port=25,
        tls="local_fixture",
        sender="s@example.com",
        allowed_recipients=("a@example.com",),
        private_networks=("127.0.0.0/8",),
    )
    with pytest.raises(EgressDenied):
        MailPolicy().validate(profile)
    profile = profile.model_copy(update={"tls": "tls"})
    with pytest.raises(EgressDenied):
        MailPolicy().validate(profile)
