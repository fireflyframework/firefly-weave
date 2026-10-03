# Copyright 2026 Firefly Software Foundation.
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
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0

"""Real local FTP, FTPS and SFTP sessions cover bounded file operations and peer verification."""

import hashlib
from uuid import uuid4

import aioftp
import asyncssh
import pytest
from firefly_weave.contracts.files import FileReference

from weave_files_worker.policy import WorkerPolicy
from weave_files_worker.providers import open_provider


async def source(data):
    yield data


async def exercise(provider):
    assert (await provider.read("hello.txt"))["sizeBytes"] == 5
    assert b"".join([data async for data in provider.download("hello.txt")]) == b"hello"
    assert len((await provider.list("", 10, None))["items"]) == 1
    file = FileReference(
        id=uuid4(),
        filename="written.txt",
        contentType="text/plain",
        sizeBytes=4,
        sha256=hashlib.sha256(b"data").hexdigest(),
    )
    await provider.write("written.txt", "written.txt", source(b"data"), file)
    await provider.move("written.txt", "moved.txt", "")
    assert (await provider.delete("moved.txt"))["deleted"] is True


async def test_real_ftp_session_stays_inside_isolated_root(tmp_path):
    (tmp_path / "hello.txt").write_bytes(b"hello")
    server = aioftp.Server([aioftp.User("user", "password", base_path=tmp_path, home_path="/")])
    await server.start("127.0.0.1", 0)
    port = server.server.sockets[0].getsockname()[1]
    origin = f"ftp://127.0.0.1:{port}"
    try:
        async with open_provider(
            "weave-ftp",
            {
                "host": "127.0.0.1",
                "port": port,
                "username": "user",
                "rootPath": "/",
                "serverRootIsolated": True,
                "destinationPolicy": "allowNonAtomic",
            },
            "password",
            WorkerPolicy(
                frozenset({origin}),
                private_networks=("127.0.0.0/8",),
                allow_cleartext_ftp=True,
                allow_non_atomic_ftp_destinations=True,
            ),
        ) as provider:
            await exercise(provider)
    finally:
        await server.close()


@pytest.mark.parametrize("connection_opt_in,worker_opt_in", [(False, False), (True, False), (False, True)])
async def test_real_ftp_write_requires_both_destination_policy_opt_ins(tmp_path, connection_opt_in, worker_opt_in):
    from firefly_weave.contracts.connectors import ConnectorFailure

    server = aioftp.Server([aioftp.User("user", "password", base_path=tmp_path, home_path="/")])
    await server.start("127.0.0.1", 0)
    port = server.server.sockets[0].getsockname()[1]
    config = {"host": "127.0.0.1", "port": port, "username": "user", "rootPath": "/", "serverRootIsolated": True}
    if connection_opt_in:
        config["destinationPolicy"] = "allowNonAtomic"
    try:
        async with open_provider(
            "weave-ftp",
            config,
            "password",
            WorkerPolicy(
                frozenset({f"ftp://127.0.0.1:{port}"}),
                private_networks=("127.0.0.0/8",),
                allow_cleartext_ftp=True,
                allow_non_atomic_ftp_destinations=worker_opt_in,
            ),
        ) as provider:
            with pytest.raises(ConnectorFailure, match="FILE_ATOMIC_DESTINATION"):
                await provider.write("invoice.pdf", "", None, None)
            with pytest.raises(ConnectorFailure, match="FILE_ATOMIC_DESTINATION"):
                await provider.move("old.pdf", "invoice.pdf", "")
        assert not list(tmp_path.iterdir())
    finally:
        await server.close()


class SSHServer(asyncssh.SSHServer):
    def begin_auth(self, username):
        return True

    def password_auth_supported(self):
        return True

    def validate_password(self, username, password):
        return username == "user" and password == "password"


async def test_real_sftp_pinned_host_and_scoped_transfer(tmp_path):
    (tmp_path / "hello.txt").write_bytes(b"hello")
    key = asyncssh.generate_private_key("ssh-ed25519")
    server = await asyncssh.create_server(
        SSHServer,
        "127.0.0.1",
        0,
        server_host_keys=[key],
        sftp_factory=lambda channel: asyncssh.SFTPServer(channel, chroot=tmp_path),
    )
    port = server.get_port()
    try:
        wrong = asyncssh.generate_private_key("ssh-ed25519")
        with pytest.raises(asyncssh.HostKeyNotVerifiable):
            async with open_provider(
                "weave-sftp",
                {
                    "host": "127.0.0.1",
                    "port": port,
                    "username": "user",
                    "rootPath": "/",
                    "serverRootIsolated": True,
                    "hostKey": wrong.export_public_key().decode().strip(),
                },
                "password",
                WorkerPolicy(frozenset({f"sftp://127.0.0.1:{port}"}), private_networks=("127.0.0.0/8",)),
            ):
                pass
        async with open_provider(
            "weave-sftp",
            {
                "host": "127.0.0.1",
                "port": port,
                "username": "user",
                "rootPath": "/",
                "serverRootIsolated": True,
                "hostKey": key.export_public_key().decode().strip(),
            },
            "password",
            WorkerPolicy(frozenset({f"sftp://127.0.0.1:{port}"}), private_networks=("127.0.0.0/8",)),
        ) as provider:
            await exercise(provider)
            (tmp_path / "link").symlink_to("hello.txt")
            from firefly_weave.contracts.connectors import ConnectorFailure

            with pytest.raises(ConnectorFailure, match="FILE_SYMLINK"):
                await provider.read("link")
    finally:
        server.close()
        await server.wait_closed()


async def test_real_ftps_secures_control_and_data_and_validates_certificate(tmp_path, monkeypatch):
    import ipaddress
    import ssl
    from datetime import UTC, datetime, timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert = tmp_path / "server.pem"
    cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    pem = tmp_path / "key.pem"
    pem.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    root = tmp_path / "root"
    root.mkdir()
    (root / "hello.txt").write_bytes(b"hello")
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, pem)

    class TLSServer(aioftp.Server):
        async def auth(self, connection, rest):
            await connection.command_connection.write(b"234 Ready for TLS\r\n")
            try:
                await connection.command_connection.writer.start_tls(tls)
            except ConnectionResetError:
                return False
            self.ssl = tls
            return True

        async def protection(self, connection, rest):
            assert connection.command_connection.writer.get_extra_info("ssl_object") is not None
            connection.response("200", "TLS data protection enabled")
            return True

    server = TLSServer([aioftp.User("user", "password", base_path=root, home_path="/")])
    server.commands_mapping.update(auth=server.auth, pbsz=server.protection, prot=server.protection)
    await server.start("127.0.0.1", 0)
    port = server.server.sockets[0].getsockname()[1]
    config = {
        "host": "127.0.0.1",
        "port": port,
        "username": "user",
        "rootPath": "/",
        "serverRootIsolated": True,
        "destinationPolicy": "allowNonAtomic",
    }
    policy = WorkerPolicy(
        frozenset({f"ftps://127.0.0.1:{port}"}),
        private_networks=("127.0.0.0/8",),
        allow_non_atomic_ftp_destinations=True,
    )
    original = ssl.create_default_context
    try:
        with pytest.raises(ssl.SSLCertVerificationError):
            async with open_provider("weave-ftps", config, "password", policy):
                pass
        monkeypatch.setattr("weave_files_worker.providers.ssl.create_default_context", lambda: original(cafile=cert))
        async with open_provider("weave-ftps", config, "password", policy) as provider:
            await exercise(provider)
    finally:
        await server.close()
