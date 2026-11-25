import datetime
import ipaddress
import os
import socket
import ssl
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, patch

import certifi
from asgiref.sync import async_to_sync
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from django.test import SimpleTestCase
from openai import APIConnectionError

from conversations.models import LLM
from billing.constants import UserWalletPreferenceTypeChoice
from core.services.custom_llm_service import CustomLLMService
from core.services.dtos import LLMDescriptor, ResolvedDispatchCredentials
from core.services.llm_service import LLMService


class _ModelHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requests += 1
        body = b'{"data": [], "object": "list"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class SpinTLSConnectionTests(SimpleTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name(
            [x509.NameAttribute(NameOID.COMMON_NAME, "spin.pace.gatech.edu")]
        )
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(hours=1))
            .add_extension(
                x509.BasicConstraints(ca=True, path_length=None), critical=True
            )
            .add_extension(
                x509.SubjectAlternativeName(
                    [
                        x509.DNSName("spin.pace.gatech.edu"),
                        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                    ]
                ),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )
        self.cert = Path(self.directory.name, "cert.pem")
        private_key = Path(self.directory.name, "key.pem")
        self.cert.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        private_key.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _ModelHandler)
        self.server.requests = 0
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(self.cert), str(private_key))
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.original_lookup = socket.getaddrinfo

    def lookup(self, host, *args, **kwargs):
        if host in ("spin.pace.gatech.edu", b"spin.pace.gatech.edu"):
            host = "127.0.0.1"
        return self.original_lookup(host, *args, **kwargs)

    def request(self, ca_file):
        endpoint = f"https://spin.pace.gatech.edu:{self.server.server_port}/v1"
        llm = LLM(name="test", identifier="test", provider="custom")
        with patch.dict(
            os.environ,
            {
                "SSL_CERT_FILE": str(ca_file),
                "NO_PROXY": "spin.pace.gatech.edu",
                "no_proxy": "spin.pace.gatech.edu",
            },
        ), patch("socket.getaddrinfo", side_effect=self.lookup):
            service = CustomLLMService(llm, api_key="test-only", base_url=endpoint)
            service.client.max_retries = 0
            try:
                return async_to_sync(service.client.models.list)()
            finally:
                async_to_sync(service.client.close)()

    def test_spin_rejects_untrusted_certificate_before_request(self):
        with self.assertRaises(APIConnectionError) as error:
            self.request(certifi.where())
        self.assertIn("CERTIFICATE_VERIFY_FAILED", str(error.exception.__cause__))
        self.assertEqual(self.server.requests, 0)

    def test_spin_accepts_trusted_certificate(self):
        response = self.request(self.cert)
        self.assertEqual(response.data, [])
        self.assertEqual(self.server.requests, 1)

    def test_spin_http_is_rejected(self):
        llm = LLM(name="test", identifier="test", provider="custom")
        with self.assertRaises(ValueError):
            CustomLLMService(
                llm, api_key="test-only", base_url="http://spin.pace.gatech.edu:4000/v1"
            )

    def test_both_spin_gemma_variants_use_custom_transport(self):
        credentials = ResolvedDispatchCredentials(
            api_key="test-only",
            base_url="https://spin.pace.gatech.edu:4000/v1",
            wallet_type=UserWalletPreferenceTypeChoice.LITELLM,
        )
        with patch(
            "core.services.llm_service.get_dispatch_credentials_for_user",
            new=AsyncMock(return_value=credentials),
        ):
            for model in ("gemma-4-26B", "gemma-4-26B-long"):
                with self.subTest(model=model):
                    descriptor = LLMDescriptor.from_litellm(object(), model, "custom")
                    service = async_to_sync(LLMService()._get_ai_service)(
                        descriptor.to_dispatch_handle(), object()
                    )
                    self.assertIsInstance(service, CustomLLMService)
                    self.assertEqual(service.model, model)
                    self.assertEqual(service.base_url, credentials.base_url)
                    async_to_sync(service.client.close)()
