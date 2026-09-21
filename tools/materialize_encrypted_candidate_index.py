from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tarfile
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

EXPECTED_INDEX_ID = "cidx-b2cd590c630213d6648665bd"
EXPECTED_BUILDER = "PUBLIC-CANDIDATE-MASS-INDEX-v1.6"
EXPECTED_LOGICAL = {
    "PUBCHEM": "79823ce75f9d9815ec0ca77c91532567b5601115b342e3ae745786e7a555133c",
    "COCONUT": "9ab38206dd6580331b5d8b71f5db549fe97d8f51a060d4d86329c545ed421ad2",
}
CHUNK_BYTES = 400_000_000


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class ChunkedEncryptedWriter:
    def __init__(self, root: Path, encryptor):
        self.root = root
        self.encryptor = encryptor
        self.root.mkdir(parents=True, exist_ok=True)
        self.chunk_index = -1
        self.handle = None
        self.chunk_bytes = 0
        self.total_cipher_bytes = 0
        self.total_plain_bytes = 0
        self.plain_hash = hashlib.sha256()
        self.cipher_hash = hashlib.sha256()
        self.chunks: list[dict] = []

    def _open_next(self):
        if self.handle is not None:
            self._close_current()
        self.chunk_index += 1
        path = self.root / f"cipher-{self.chunk_index:03d}.bin"
        self.handle = path.open("wb")
        self.chunk_bytes = 0

    def _close_current(self):
        if self.handle is None:
            return
        path = Path(self.handle.name)
        self.handle.close()
        self.chunks.append(
            {
                "name": path.name,
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
        self.handle = None

    def _write_cipher(self, data: bytes):
        view = memoryview(data)
        while view:
            if self.handle is None:
                self._open_next()
            remaining = CHUNK_BYTES - self.chunk_bytes
            piece = view[:remaining]
            self.handle.write(piece)
            n = len(piece)
            self.chunk_bytes += n
            self.total_cipher_bytes += n
            self.cipher_hash.update(piece)
            view = view[n:]
            if self.chunk_bytes >= CHUNK_BYTES:
                self._close_current()

    def write(self, data: bytes):
        self.total_plain_bytes += len(data)
        self.plain_hash.update(data)
        encrypted = self.encryptor.update(data)
        if encrypted:
            self._write_cipher(encrypted)
        return len(data)

    def flush(self):
        if self.handle is not None:
            self.handle.flush()

    def finalize(self):
        tail = self.encryptor.finalize()
        if tail:
            self._write_cipher(tail)
        self._close_current()


def load_builder():
    path = Path("tools/public_candidate_index_build.py")
    spec = importlib.util.spec_from_file_location("accepted_candidate_builder", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    builder = load_builder()
    if builder.BUILDER_VERSION != EXPECTED_BUILDER:
        raise SystemExit(
            f"BUILDER_VERSION_MISMATCH:{builder.BUILDER_VERSION}:{EXPECTED_BUILDER}"
        )

    original_rmtree = builder.shutil.rmtree

    def preserve_work(path, *args, **kwargs):
        if Path(path).name == "candidate-index-work":
            print("MATERIALIZATION_TRANSPORT preserving candidate-index-work", flush=True)
            return
        return original_rmtree(path, *args, **kwargs)

    builder.shutil.rmtree = preserve_work
    rc = builder.main()
    builder.shutil.rmtree = original_rmtree
    if rc != 0:
        raise SystemExit(rc)

    receipt_path = Path("results/candidate-index-build-receipt.json")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt.get("state") != "PASS":
        raise SystemExit("BUILD_RECEIPT_NOT_PASS")
    if receipt.get("candidate_index_id") != EXPECTED_INDEX_ID:
        raise SystemExit("CANDIDATE_INDEX_ID_MISMATCH")
    observed = {
        row["source"]: row["logical_index_sha256"] for row in receipt["sources"]
    }
    if observed != EXPECTED_LOGICAL:
        raise SystemExit(f"LOGICAL_INDEX_HASH_MISMATCH:{observed}")

    public_key_bytes = Path("keys/candidate-index-materialization-public.pem").read_bytes()
    public_key = serialization.load_pem_public_key(public_key_bytes)
    public_key_der = public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    public_key_fingerprint = hashlib.sha256(public_key_der).hexdigest()

    aes_key = os.urandom(32)
    nonce = os.urandom(12)
    encryptor = Cipher(algorithms.AES(aes_key), modes.GCM(nonce)).encryptor()

    envelope_root = Path("materialization-envelope")
    if envelope_root.exists():
        shutil.rmtree(envelope_root)
    chunks_root = envelope_root / "chunks"
    writer = ChunkedEncryptedWriter(chunks_root, encryptor)

    work = Path("candidate-index-work")
    if not work.is_dir():
        raise SystemExit("CANDIDATE_INDEX_WORK_NOT_PRESERVED")

    with tarfile.open(fileobj=writer, mode="w|") as tf:
        tf.add(work, arcname="candidate-index-work", recursive=True)
        tf.add(receipt_path, arcname="candidate-index-build-receipt.json")
    writer.finalize()

    encrypted_key = public_key.encrypt(
        aes_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )

    manifest = {
        "envelope_version": "1.0.0",
        "state": "PASS",
        "transport": "RSA-OAEP-SHA256 + AES-256-GCM",
        "candidate_index_id": EXPECTED_INDEX_ID,
        "builder_version": EXPECTED_BUILDER,
        "source_logical_index_sha256": EXPECTED_LOGICAL,
        "public_key_fingerprint_sha256": public_key_fingerprint,
        "encrypted_aes_key_b64": base64.b64encode(encrypted_key).decode("ascii"),
        "nonce_b64": base64.b64encode(nonce).decode("ascii"),
        "tag_b64": base64.b64encode(encryptor.tag).decode("ascii"),
        "plaintext_tar_sha256": writer.plain_hash.hexdigest(),
        "plaintext_tar_bytes": writer.total_plain_bytes,
        "ciphertext_sha256": writer.cipher_hash.hexdigest(),
        "ciphertext_bytes": writer.total_cipher_bytes,
        "chunk_bytes_target": CHUNK_BYTES,
        "chunks": writer.chunks,
        "plaintext_payload_uploaded": False,
        "ciphertext_only_artifact": True,
        "accepted_build_receipt_sha256": sha256_file(receipt_path),
    }
    (envelope_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )

    shutil.rmtree(work)
    print(
        json.dumps(
            {
                "state": manifest["state"],
                "candidate_index_id": manifest["candidate_index_id"],
                "public_key_fingerprint_sha256": public_key_fingerprint,
                "plaintext_tar_sha256": manifest["plaintext_tar_sha256"],
                "plaintext_tar_bytes": manifest["plaintext_tar_bytes"],
                "ciphertext_sha256": manifest["ciphertext_sha256"],
                "ciphertext_bytes": manifest["ciphertext_bytes"],
                "chunk_count": len(manifest["chunks"]),
                "chunks": manifest["chunks"],
                "plaintext_payload_uploaded": False,
                "ciphertext_only_artifact": True,
            },
            indent=2,
            sort_keys=True,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
