from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

CHUNK = 4 * 1024 * 1024
PUBLIC_KEY = Path("keys/wr02-successor-snapshot-public.pem")
TRANSPORT = Path("wr02-successor-transport")
META = TRANSPORT / "meta"

SOURCES = {
    "CID-SMILES.gz": {
        "url": "https://ftp.ncbi.nlm.nih.gov/pubchem/Compound/Extras/CID-SMILES.gz",
        "sha256": "a54f09282b0a4cf0bee5dad29241c1b690134f2b9c157a8a4e557247b616bbcf",
        "bytes": 1486154982,
        "source": "PUBCHEM",
    },
    "CID-InChI-Key.gz": {
        "url": "https://ftp.ncbi.nlm.nih.gov/pubchem/Compound/Extras/CID-InChI-Key.gz",
        "sha256": "a9058746a18486178a438bd855ab7bfaf24fdf71696aa08686ad67110a54d127",
        "bytes": 7366433520,
        "source": "PUBCHEM",
    },
    "CID-Mass.gz": {
        "url": "https://ftp.ncbi.nlm.nih.gov/pubchem/Compound/Extras/CID-Mass.gz",
        "sha256": "0792f62a2788dadf97c142dffb9f1fcec22646f49a5dbb4de9b223d9587119a2",
        "bytes": 1391114576,
        "source": "PUBCHEM",
    },
    "coconut_csv-09-2026.zip": {
        "url": "https://coconut.s3.uni-jena.de/prod/downloads/2026-09/coconut_csv-09-2026.zip",
        "sha256": "38b8a3a73a40c90239ff4d5b0caf832981fd3029f4c8fc0f43c5011b39461800",
        "bytes": 247982286,
        "source": "COCONUT",
    },
}


def safe(name: str) -> str:
    return name.replace(".", "_").replace("-", "_")


def load_public_key():
    raw = PUBLIC_KEY.read_bytes()
    key = serialization.load_pem_public_key(raw)
    der = key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return key, hashlib.sha256(der).hexdigest()


def encrypt_source(name: str) -> int:
    spec = SOURCES[name]
    TRANSPORT.mkdir(parents=True, exist_ok=True)
    META.mkdir(parents=True, exist_ok=True)
    out_path = TRANSPORT / f"{safe(name)}.cipher"

    public_key, fingerprint = load_public_key()
    aes_key = os.urandom(32)
    nonce = os.urandom(12)
    enc = Cipher(algorithms.AES(aes_key), modes.GCM(nonce)).encryptor()

    source_hash = hashlib.sha256()
    cipher_hash = hashlib.sha256()
    source_bytes = 0
    cipher_bytes = 0

    headers = {}
    with requests.get(
        spec["url"],
        stream=True,
        timeout=(30, 1800),
        allow_redirects=True,
        headers={"User-Agent": "spectravane-wr02-durable-snapshot/1.0"},
    ) as response:
        response.raise_for_status()
        headers = {
            "status_code": response.status_code,
            "content_length": response.headers.get("content-length"),
            "etag": response.headers.get("etag"),
            "last_modified": response.headers.get("last-modified"),
            "content_type": response.headers.get("content-type"),
            "final_url": response.url,
        }
        with out_path.open("wb") as out:
            for block in response.iter_content(CHUNK):
                if not block:
                    continue
                source_hash.update(block)
                source_bytes += len(block)
                cipher = enc.update(block)
                out.write(cipher)
                cipher_hash.update(cipher)
                cipher_bytes += len(cipher)
            tail = enc.finalize()
            if tail:
                out.write(tail)
                cipher_hash.update(tail)
                cipher_bytes += len(tail)

    observed = source_hash.hexdigest()
    if observed != spec["sha256"]:
        out_path.unlink(missing_ok=True)
        raise SystemExit(f"SOURCE_HASH_MISMATCH:{name}:{observed}")
    if source_bytes != spec["bytes"]:
        out_path.unlink(missing_ok=True)
        raise SystemExit(f"SOURCE_BYTE_COUNT_MISMATCH:{name}:{source_bytes}")

    wrapped = public_key.encrypt(
        aes_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    row = {
        "name": name,
        "source": spec["source"],
        "url": spec["url"],
        "expected_source_sha256": spec["sha256"],
        "source_sha256": observed,
        "source_bytes": source_bytes,
        "http": headers,
        "cipher_file": out_path.name,
        "cipher_sha256": cipher_hash.hexdigest(),
        "cipher_bytes": cipher_bytes,
        "transport": "RSA-OAEP-SHA256 + AES-256-GCM",
        "public_key_fingerprint_sha256": fingerprint,
        "encrypted_aes_key_b64": base64.b64encode(wrapped).decode("ascii"),
        "nonce_b64": base64.b64encode(nonce).decode("ascii"),
        "tag_b64": base64.b64encode(enc.tag).decode("ascii"),
        "plaintext_payload_uploaded": False,
        "ciphertext_only_public_artifact": True,
    }
    (META / f"{safe(name)}.json").write_text(
        json.dumps(row, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps({k: v for k, v in row.items() if not k.endswith("_b64")}, indent=2, sort_keys=True))
    return 0


def assemble() -> int:
    rows = []
    for name in SOURCES:
        p = META / f"{safe(name)}.json"
        if not p.is_file():
            raise SystemExit(f"MISSING_SOURCE_METADATA:{name}")
        rows.append(json.loads(p.read_text(encoding="utf-8")))
    manifest = {
        "manifest_version": "1.0.0",
        "state": "PASS",
        "snapshot_class": "WR02_SUCCESSOR_CURRENT_SOURCE_SET_2026_09_26",
        "public_key_fingerprint_sha256": rows[0]["public_key_fingerprint_sha256"],
        "sources": rows,
        "pubchem_coordinated_fileset_sha256": hashlib.sha256(
            json.dumps(
                [
                    {
                        "name": r["name"],
                        "archive_sha256": r["source_sha256"],
                        "byte_size": r["source_bytes"],
                    }
                    for r in rows if r["source"] == "PUBCHEM"
                ],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
        "coconut_unchanged_from_2026_09_21": (
            next(r for r in rows if r["source"] == "COCONUT")["source_sha256"]
            == "38b8a3a73a40c90239ff4d5b0caf832981fd3029f4c8fc0f43c5011b39461800"
        ),
        "plaintext_payload_uploaded": False,
        "recovery_requirement": "Persist all ciphertext artifacts and the transport recovery key in private durable storage before declaring WR-02 durable recovery PASS.",
    }
    (TRANSPORT / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps({
        "state": manifest["state"],
        "snapshot_class": manifest["snapshot_class"],
        "pubchem_coordinated_fileset_sha256": manifest["pubchem_coordinated_fileset_sha256"],
        "coconut_unchanged_from_2026_09_21": manifest["coconut_unchanged_from_2026_09_21"],
        "source_count": len(rows),
    }, indent=2, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    enc = sub.add_parser("encrypt")
    enc.add_argument("--name", choices=tuple(SOURCES), required=True)
    sub.add_parser("assemble")
    args = parser.parse_args()
    if args.command == "encrypt":
        return encrypt_source(args.name)
    return assemble()


if __name__ == "__main__":
    raise SystemExit(main())
