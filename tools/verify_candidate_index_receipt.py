from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

HEX64 = re.compile(r"^[0-9a-f]{64}$")
EXPECTED_BUILDER = "PUBLIC-CANDIDATE-MASS-INDEX-v1.6"
EXPECTED_SOURCE_IDENTITIES = {
    "PUBCHEM": "PUBCHEM_EXTRAS_CID_SMILES_AND_MASS_FROZEN_HASHES",
    "COCONUT": "COCONUT_2026_09_FULL_CSV_FROZEN_HASH",
}
EXPECTED_SOURCE_HASHES = {
    "PUBCHEM": {
        "CID-SMILES.gz": "abedf7b9709e98a1de9af5e3bea20d839c9953b5862157e6a6aafe55e15eb8c9",
        "CID-Mass.gz": "fd81bc739efb0d574df29188068ee9a9b29552d2d3167e71eda7984b9c6a545b",
    },
    "COCONUT": {
        "archive_sha256": "38b8a3a73a40c90239ff4d5b0caf832981fd3029f4c8fc0f43c5011b39461800",
    },
}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def validate(receipt: dict) -> list[str]:
    errors: list[str] = []
    if receipt.get("state") != "PASS":
        errors.append("STATE_NOT_PASS")
    if receipt.get("builder_version") != EXPECTED_BUILDER:
        errors.append("BUILDER_VERSION_MISMATCH")
    if receipt.get("mass_range_da") != [0.0, 1500.0]:
        errors.append("MASS_RANGE_MISMATCH")
    if receipt.get("bin_width_da") != 25:
        errors.append("BIN_WIDTH_MISMATCH")
    if not str(receipt.get("candidate_index_id", "")).startswith("cidx-"):
        errors.append("INDEX_ID_INVALID")
    if receipt.get("artifact_policy", {}).get("index_payload_uploaded") is not False:
        errors.append("INDEX_PAYLOAD_PUBLICLY_UPLOADED")
    if receipt.get("index_semantics", {}).get("production_rights_inferred") is not False:
        errors.append("RIGHTS_INFERENCE_FORBIDDEN")

    sources = {row.get("source"): row for row in receipt.get("sources", [])}
    if set(sources) != set(EXPECTED_SOURCE_IDENTITIES):
        errors.append("SOURCE_SET_MISMATCH")

    for source_id, expected_identity in EXPECTED_SOURCE_IDENTITIES.items():
        row = sources.get(source_id)
        if row is None:
            continue
        if row.get("source_identity") != expected_identity:
            errors.append(f"{source_id}_IDENTITY_MISMATCH")
        if row.get("format") != "MASS_BUCKET_TSV_GZIP_V1":
            errors.append(f"{source_id}_FORMAT_MISMATCH")
        if row.get("shard_count") != len(row.get("shards", [])):
            errors.append(f"{source_id}_SHARD_COUNT_MISMATCH")
        if row.get("counts", {}).get("indexed_records") != sum(
            int(x.get("row_count", 0)) for x in row.get("shards", [])
        ):
            errors.append(f"{source_id}_ROW_COUNT_MISMATCH")
        if row.get("total_compressed_index_bytes") != sum(
            int(x.get("compressed_bytes", 0)) for x in row.get("shards", [])
        ):
            errors.append(f"{source_id}_BYTE_COUNT_MISMATCH")
        if row.get("readback_profile", {}).get("state") != "PASS":
            errors.append(f"{source_id}_READBACK_NOT_PASS")
        for field in ("logical_index_sha256", "compressed_index_sha256"):
            if not HEX64.fullmatch(str(row.get(field, ""))):
                errors.append(f"{source_id}_{field.upper()}_INVALID")
        for shard in row.get("shards", []):
            if not HEX64.fullmatch(str(shard.get("logical_sha256", ""))):
                errors.append(f"{source_id}_SHARD_LOGICAL_HASH_INVALID")
                break
            if not HEX64.fullmatch(str(shard.get("compressed_sha256", ""))):
                errors.append(f"{source_id}_SHARD_COMPRESSED_HASH_INVALID")
                break

    if "PUBCHEM" in sources:
        verification = sources["PUBCHEM"].get("source_verification", {})
        for name, expected in EXPECTED_SOURCE_HASHES["PUBCHEM"].items():
            if verification.get(name, {}).get("archive_sha256") != expected:
                errors.append(f"PUBCHEM_SOURCE_HASH_MISMATCH:{name}")
    if "COCONUT" in sources:
        observed = sources["COCONUT"].get("source_verification", {}).get("archive_sha256")
        if observed != EXPECTED_SOURCE_HASHES["COCONUT"]["archive_sha256"]:
            errors.append("COCONUT_SOURCE_HASH_MISMATCH")
    return errors


def main() -> int:
    path = Path("imported/candidate-index-build-receipt.json")
    receipt = json.loads(path.read_text(encoding="utf-8"))
    errors = validate(receipt)
    sources = {row["source"]: row for row in receipt.get("sources", [])}
    attestation = {
        "attestation_version": "1.0.0",
        "state": "PASS" if not errors else "FAIL",
        "receipt_sha256": sha256_file(path),
        "candidate_index_id": receipt.get("candidate_index_id"),
        "builder_version": receipt.get("builder_version"),
        "build_seconds_total": receipt.get("build_seconds_total"),
        "disk_usage_bytes": receipt.get("disk_usage_bytes"),
        "python_version": receipt.get("python_version"),
        "platform": receipt.get("platform"),
        "artifact_policy": receipt.get("artifact_policy"),
        "index_semantics": receipt.get("index_semantics"),
        "sources": {
            key: {
                "source_identity": row.get("source_identity"),
                "counts": row.get("counts"),
                "logical_index_sha256": row.get("logical_index_sha256"),
                "compressed_index_sha256": row.get("compressed_index_sha256"),
                "total_compressed_index_bytes": row.get("total_compressed_index_bytes"),
                "shard_count": row.get("shard_count"),
                "readback_profile": row.get("readback_profile"),
                "source_verification": row.get("source_verification"),
            }
            for key, row in sorted(sources.items())
        },
        "validation_errors": errors,
    }
    Path("results").mkdir(exist_ok=True)
    out = Path("results/candidate-index-attestation.json")
    out.write_text(json.dumps(attestation, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(attestation, indent=2, sort_keys=True))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
