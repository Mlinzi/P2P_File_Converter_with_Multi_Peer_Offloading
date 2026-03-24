#!/usr/bin/env python3
"""
Generate a self-signed TLS certificate for P2P Convert.

Run once before enabling TLS:
    python certs/gen_cert.py

Then copy the certs/ folder to all peers so they share the same certificate.
The client skips CA verification (self-signed), so every peer just needs the
same cert+key pair to interoperate.
"""

import datetime
import sys
from pathlib import Path

CERT_DIR  = Path(__file__).parent
CERT_FILE = CERT_DIR / 'peer.crt'
KEY_FILE  = CERT_DIR / 'peer.key'


def gen_cert():
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
    except ImportError:
        print("Install cryptography first:  pip install cryptography")
        sys.exit(1)

    print("Generating 2048-bit RSA key...")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME,       'p2pconvert'),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, 'P2PConvert-LAN'),
    ])

    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.utcnow())
        .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    KEY_FILE.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    CERT_FILE.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    print(f"  cert  ->  {CERT_FILE}")
    print(f"  key   ->  {KEY_FILE}")
    print()
    print("Copy the entire certs/ folder to all peers so they share the same cert.")
    print("Then launch any peer with:  python peer.py --tls")


if __name__ == '__main__':
    if CERT_FILE.exists() and KEY_FILE.exists():
        print("Certs already exist:")
        print(f"  {CERT_FILE}")
        print(f"  {KEY_FILE}")
        print("Delete them to regenerate.")
        sys.exit(0)
    gen_cert()
