from arango.database import StandardDatabase
from pytest import fixture
from resotolib.x509 import bootstrap_ca, load_cert_from_bytes, cert_fingerprint, csr_to_bytes, gen_csr, gen_rsa_key

from resotocore.dependencies import parse_args
from resotocore.web.certificate_handler import CertificateHandler

# noinspection PyUnresolvedReferences
from tests.resotocore.db.graphdb_test import test_db, system_db, local_client


@fixture
def cert_handler() -> CertificateHandler:
    args = parse_args()
    key, certificate = bootstrap_ca()
    return CertificateHandler(args, key, certificate)


def test_ca_certificate(cert_handler: CertificateHandler) -> None:
    cert_bytes, fingerprint = cert_handler.authority_certificate
    cert = load_cert_from_bytes(cert_bytes)
    assert cert_fingerprint(cert) == fingerprint


def test_sign(cert_handler: CertificateHandler) -> None:
    cert_bytes, fingerprint = cert_handler.sign(csr_to_bytes(gen_csr(gen_rsa_key())))
    cert = load_cert_from_bytes(cert_bytes)
    assert cert_fingerprint(cert) == fingerprint


def test_bootstrap(test_db: StandardDatabase) -> None:
    sd = test_db.collection("system_data")
    args = parse_args()
    # Delete any existing entry, so a new certificate needs to be created
    sd.delete("ca", ignore_missing=True)
    handler = CertificateHandler.lookup(args, test_db)
    ca = sd.get("ca")
    assert ca is not None
    # ensure the certificate in the database is the same as exposed by the handler
    ca_bytes, fingerprint = handler.authority_certificate
    assert ca_bytes == ca["certificate"].encode("utf-8")
    # a new handler will use the existing certificate
    handler2 = CertificateHandler.lookup(args, test_db)
    assert handler.authority_certificate == handler2.authority_certificate
    # but the host certificate will be different
    assert handler.host_certificate != handler2.host_certificate
