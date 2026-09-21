from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import math
import os
import platform
import shutil
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

import requests

BUILDER_VERSION = "PUBLIC-CANDIDATE-MASS-INDEX-v1.4"
BIN_WIDTH_DA = 25
MIN_MASS_DA = 0.0
MAX_MASS_DA = 1500.0
CHUNK = 4 * 1024 * 1024

PUBCHEM_SMILES_URL = "https://ftp.ncbi.nlm.nih.gov/pubchem/Compound/Extras/CID-SMILES.gz"
PUBCHEM_MASS_URL = "https://ftp.ncbi.nlm.nih.gov/pubchem/Compound/Extras/CID-Mass.gz"
PUBCHEM_SMILES_SHA256 = "abedf7b9709e98a1de9af5e3bea20d839c9953b5862157e6a6aafe55e15eb8c9"
PUBCHEM_MASS_SHA256 = "fd81bc739efb0d574df29188068ee9a9b29552d2d3167e71eda7984b9c6a545b"
PUBCHEM_SNAPSHOT_ID = "snap-3e0cea6b6f6f2f9b21603519"
PUBCHEM_SOURCE_IDENTITY = "PUBCHEM_EXTRAS_CID_SMILES_AND_MASS_FROZEN_HASHES"

COCONUT_URL = (
    "https://coconut.s3.uni-jena.de/prod/downloads/2026-09/"
    "coconut_csv-09-2026.zip"
)
COCONUT_SHA256 = "38b8a3a73a40c90239ff4d5b0caf832981fd3029f4c8fc0f43c5011b39461800"
COCONUT_SOURCE_IDENTITY = "COCONUT_2026_09_FULL_CSV_FROZEN_HASH"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_text(value: str | None) -> str:
    if value is None:
        return ""
    return (
        value.replace("\\", "\\\\")
        .replace("\t", "\\t")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )


class HashingReader:
    def __init__(self, raw):
        self.raw = raw
        self.hash = hashlib.sha256()
        self.byte_count = 0

    def read(self, size=-1):
        data = self.raw.read(size)
        if data:
            self.hash.update(data)
            self.byte_count += len(data)
        return data

    def close(self):
        self.raw.close()


@dataclass
class StreamRow:
    cid: int
    fields: list[str]


class GzipTSVStream:
    def __init__(self, session: requests.Session, url: str):
        self.response = session.get(url, stream=True, timeout=(30, 1200), allow_redirects=True)
        self.response.raise_for_status()
        self.response.raw.decode_content = False
        self.reader = HashingReader(self.response.raw)
        self.gz = gzip.GzipFile(fileobj=self.reader, mode="rb")
        self.text = io.TextIOWrapper(self.gz, encoding="utf-8", newline="")
        self.extracted_hash = hashlib.sha256()
        self.uncompressed_bytes = 0
        self.line_count = 0

    def next(self) -> StreamRow | None:
        line = self.text.readline()
        if not line:
            return None
        raw = line.encode("utf-8")
        self.extracted_hash.update(raw)
        self.uncompressed_bytes += len(raw)
        self.line_count += 1
        parts = line.rstrip("\r\n").split("\t")
        return StreamRow(int(parts[0]), parts[1:])

    def finish(self) -> dict:
        # Drain any unread bytes so source hashes always cover the exact archive.
        while True:
            line = self.text.readline()
            if not line:
                break
            raw = line.encode("utf-8")
            self.extracted_hash.update(raw)
            self.uncompressed_bytes += len(raw)
            self.line_count += 1
        self.text.close()
        return {
            "archive_sha256": self.reader.hash.hexdigest(),
            "compressed_bytes": self.reader.byte_count,
            "extracted_content_sha256": self.extracted_hash.hexdigest(),
            "uncompressed_bytes": self.uncompressed_bytes,
            "line_count": self.line_count,
            "last_modified": self.response.headers.get("last-modified"),
            "content_length": self.response.headers.get("content-length"),
        }


class MassBucketWriter:
    BUFFER_BYTES = 4 * 1024 * 1024

    def __init__(self, root: Path, source: str):
        self.root = root / source.lower()
        self.root.mkdir(parents=True, exist_ok=True)
        self.source = source
        self.handles: dict[int, tuple[object, gzip.GzipFile]] = {}
        self.buffers: dict[int, bytearray] = {}
        self.stats: dict[int, dict] = {}

    def _bucket(self, mass: float) -> int:
        return int(math.floor(mass / BIN_WIDTH_DA) * BIN_WIDTH_DA)

    def _ensure(self, bucket: int) -> None:
        if bucket in self.handles:
            return
        path = self.root / f"mass-{bucket:04d}-{bucket + BIN_WIDTH_DA:04d}.tsv.gz"
        raw = path.open("wb")
        gz = gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=1, mtime=0)
        self.handles[bucket] = (raw, gz)
        self.buffers[bucket] = bytearray()
        self.stats[bucket] = {
            "bucket_min_da": bucket,
            "bucket_max_da_exclusive": bucket + BIN_WIDTH_DA,
            "path": str(path),
            "row_count": 0,
            "logical_sha256_state": hashlib.sha256(),
        }

    def _flush(self, bucket: int) -> None:
        buf = self.buffers[bucket]
        if not buf:
            return
        self.handles[bucket][1].write(buf)
        buf.clear()

    def write(self, mass: float, fields: list[str]) -> None:
        bucket = self._bucket(mass)
        self._ensure(bucket)
        line = ("\t".join(safe_text(x) for x in fields) + "\n").encode("utf-8")
        buf = self.buffers[bucket]
        buf.extend(line)
        if len(buf) >= self.BUFFER_BYTES:
            self._flush(bucket)
        stat = self.stats[bucket]
        stat["row_count"] += 1
        stat["logical_sha256_state"].update(line)

    def close(self) -> list[dict]:
        for bucket in list(self.handles):
            self._flush(bucket)
        for raw, gz in self.handles.values():
            gz.close()
            raw.close()
        out = []
        for bucket in sorted(self.stats):
            stat = self.stats[bucket]
            path = Path(stat["path"])
            h = hashlib.sha256()
            with path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(CHUNK), b""):
                    h.update(chunk)
            out.append(
                {
                    "bucket_min_da": stat["bucket_min_da"],
                    "bucket_max_da_exclusive": stat["bucket_max_da_exclusive"],
                    "filename": path.name,
                    "row_count": stat["row_count"],
                    "logical_sha256": stat["logical_sha256_state"].hexdigest(),
                    "compressed_sha256": h.hexdigest(),
                    "compressed_bytes": path.stat().st_size,
                }
            )
        return out


def build_pubchem(session: requests.Session, root: Path) -> dict:
    started = time.time()
    smiles_stream = GzipTSVStream(session, PUBCHEM_SMILES_URL)
    mass_stream = GzipTSVStream(session, PUBCHEM_MASS_URL)
    writer = MassBucketWriter(root, "PUBCHEM")

    s = smiles_stream.next()
    m = mass_stream.next()
    joined = 0
    emitted = 0
    missing_smiles = 0
    missing_mass = 0
    invalid_mass = 0
    out_of_range = 0

    while s is not None or m is not None:
        if m is None or (s is not None and s.cid < m.cid):
            missing_mass += 1
            s = smiles_stream.next()
            continue
        if s is None or m.cid < s.cid:
            missing_smiles += 1
            m = mass_stream.next()
            continue

        joined += 1
        if joined % 5_000_000 == 0:
            print(f"PUBCHEM_PROGRESS joined={joined:,} emitted={emitted:,}", flush=True)
        cid = s.cid
        smiles = s.fields[0] if s.fields else ""
        formula = m.fields[0] if len(m.fields) >= 1 else ""
        mono_text = m.fields[1] if len(m.fields) >= 2 else ""
        try:
            mono = float(mono_text)
        except Exception:
            invalid_mass += 1
            s = smiles_stream.next()
            m = mass_stream.next()
            continue

        if MIN_MASS_DA <= mono <= MAX_MASS_DA:
            writer.write(
                mono,
                [
                    str(cid),
                    mono_text,
                    formula,
                    smiles,
                ],
            )
            emitted += 1
        else:
            out_of_range += 1

        s = smiles_stream.next()
        m = mass_stream.next()

    smiles_meta = smiles_stream.finish()
    mass_meta = mass_stream.finish()
    shards = writer.close()

    if smiles_meta["archive_sha256"] != PUBCHEM_SMILES_SHA256:
        raise RuntimeError(
            f"PUBCHEM_SMILES_HASH_MISMATCH:{smiles_meta['archive_sha256']}"
        )
    if mass_meta["archive_sha256"] != PUBCHEM_MASS_SHA256:
        raise RuntimeError(
            f"PUBCHEM_MASS_HASH_MISMATCH:{mass_meta['archive_sha256']}"
        )

    return {
        "source": "PUBCHEM",
        "source_identity": PUBCHEM_SOURCE_IDENTITY,
        "builder_version": BUILDER_VERSION,
        "format": "MASS_BUCKET_TSV_GZIP_V1",
        "fields": ["cid", "monoisotopic_mass", "formula", "smiles"],
        "mass_range_da": [MIN_MASS_DA, MAX_MASS_DA],
        "bin_width_da": BIN_WIDTH_DA,
        "source_verification": {
            "CID-SMILES.gz": smiles_meta,
            "CID-Mass.gz": mass_meta,
        },
        "counts": {
            "joined_records": joined,
            "indexed_records": emitted,
            "missing_smiles": missing_smiles,
            "missing_mass": missing_mass,
            "invalid_mass": invalid_mass,
            "out_of_range": out_of_range,
        },
        "shards": shards,
        "build_seconds": time.time() - started,
    }


def download_verified(session: requests.Session, url: str, expected_hash: str, path: Path) -> dict:
    h = hashlib.sha256()
    total = 0
    with session.get(url, stream=True, timeout=(30, 1200), allow_redirects=True) as r:
        r.raise_for_status()
        with path.open("wb") as out:
            for chunk in r.iter_content(CHUNK):
                if not chunk:
                    continue
                h.update(chunk)
                total += len(chunk)
                out.write(chunk)
        meta = {
            "status_code": r.status_code,
            "last_modified": r.headers.get("last-modified"),
            "etag": r.headers.get("etag"),
            "content_length": r.headers.get("content-length"),
            "archive_sha256": h.hexdigest(),
            "compressed_bytes": total,
        }
    if meta["archive_sha256"] != expected_hash:
        path.unlink(missing_ok=True)
        raise RuntimeError(f"SOURCE_HASH_MISMATCH:{path.name}:{meta['archive_sha256']}")
    return meta


def build_coconut(session: requests.Session, root: Path) -> dict:
    started = time.time()
    writer = MassBucketWriter(root, "COCONUT")
    with tempfile.TemporaryDirectory() as td:
        zip_path = Path(td) / "coconut.zip"
        source_meta = download_verified(session, COCONUT_URL, COCONUT_SHA256, zip_path)
        with zipfile.ZipFile(zip_path) as zf:
            members = [x for x in zf.infolist() if not x.is_dir()]
            if len(members) != 1 or not members[0].filename.lower().endswith(".csv"):
                raise RuntimeError("COCONUT_UNEXPECTED_ARCHIVE_LAYOUT")
            info = members[0]
            p = Path(info.filename)
            if p.is_absolute() or ".." in p.parts or "\\" in info.filename:
                raise RuntimeError("COCONUT_UNSAFE_ARCHIVE_MEMBER")
            with zf.open(info, "r") as raw:
                text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
                reader = csv.DictReader(text)
                required = {
                    "identifier",
                    "canonical_smiles",
                    "standard_inchi_key",
                    "exact_molecular_weight",
                    "molecular_formula",
                    "collections",
                    "dois",
                }
                missing = required - set(reader.fieldnames or [])
                if missing:
                    raise RuntimeError(f"COCONUT_SCHEMA_MISSING:{sorted(missing)}")
                total = 0
                emitted = 0
                invalid_mass = 0
                out_of_range = 0
                for row in reader:
                    total += 1
                    if total % 1_000_000 == 0:
                        print(f"COCONUT_PROGRESS rows={total:,} emitted={emitted:,}", flush=True)
                    try:
                        mass = float(row["exact_molecular_weight"])
                    except Exception:
                        invalid_mass += 1
                        continue
                    if not (MIN_MASS_DA <= mass <= MAX_MASS_DA):
                        out_of_range += 1
                        continue
                    writer.write(
                        mass,
                        [
                            row["identifier"],
                            row["exact_molecular_weight"],
                            row["molecular_formula"],
                            row["canonical_smiles"],
                            row["standard_inchi_key"],
                            row.get("collections", ""),
                            row.get("dois", ""),
                        ],
                    )
                    emitted += 1
    shards = writer.close()
    return {
        "source": "COCONUT",
        "source_identity": COCONUT_SOURCE_IDENTITY,
        "builder_version": BUILDER_VERSION,
        "format": "MASS_BUCKET_TSV_GZIP_V1",
        "fields": [
            "identifier",
            "exact_molecular_weight",
            "formula",
            "canonical_smiles",
            "standard_inchi_key",
            "collections",
            "dois",
        ],
        "mass_range_da": [MIN_MASS_DA, MAX_MASS_DA],
        "bin_width_da": BIN_WIDTH_DA,
        "source_verification": source_meta,
        "counts": {
            "source_records": total,
            "indexed_records": emitted,
            "invalid_mass": invalid_mass,
            "out_of_range": out_of_range,
        },
        "shards": shards,
        "build_seconds": time.time() - started,
    }


def finalize_source(source: dict) -> dict:
    logical_basis = [
        {
            "bucket_min_da": s["bucket_min_da"],
            "row_count": s["row_count"],
            "logical_sha256": s["logical_sha256"],
        }
        for s in source["shards"]
    ]
    compressed_basis = [
        {
            "bucket_min_da": s["bucket_min_da"],
            "compressed_bytes": s["compressed_bytes"],
            "compressed_sha256": s["compressed_sha256"],
        }
        for s in source["shards"]
    ]
    source["logical_index_sha256"] = sha256_bytes(
        json.dumps(logical_basis, sort_keys=True, separators=(",", ":")).encode()
    )
    source["compressed_index_sha256"] = sha256_bytes(
        json.dumps(compressed_basis, sort_keys=True, separators=(",", ":")).encode()
    )
    source["total_compressed_index_bytes"] = sum(
        x["compressed_bytes"] for x in source["shards"]
    )
    source["shard_count"] = len(source["shards"])
    return source


def profile_readback(root: Path, source: dict) -> dict:
    shards = source["shards"]
    if not shards:
        return {"state": "NO_SHARDS"}
    ordered = sorted(shards, key=lambda x: x["row_count"])
    selected = [ordered[0], ordered[len(ordered) // 2], ordered[-1]]
    results = []
    for shard in selected:
        path = root / source["source"].lower() / shard["filename"]
        started = time.perf_counter()
        rows = 0
        logical = hashlib.sha256()
        with gzip.open(path, "rb") as fh:
            for line in fh:
                logical.update(line)
                rows += 1
        seconds = time.perf_counter() - started
        results.append(
            {
                "filename": shard["filename"],
                "row_count": rows,
                "seconds": seconds,
                "rows_per_second": rows / seconds if seconds else None,
                "logical_sha256_matches": logical.hexdigest() == shard["logical_sha256"],
            }
        )
    return {
        "state": "PASS" if all(x["logical_sha256_matches"] for x in results) else "FAIL",
        "selected_shards": results,
    }


def main() -> int:
    started = time.time()
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "public-candidate-index-builder/1.0 "
                "(public-source research reproducibility)"
            )
        }
    )
    work = Path("candidate-index-work")
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()

    pubchem = finalize_source(build_pubchem(session, work))
    coconut = finalize_source(build_coconut(session, work))
    pubchem["readback_profile"] = profile_readback(work, pubchem)
    coconut["readback_profile"] = profile_readback(work, coconut)

    identity_basis = {
        "builder_version": BUILDER_VERSION,
        "mass_range_da": [MIN_MASS_DA, MAX_MASS_DA],
        "bin_width_da": BIN_WIDTH_DA,
        "sources": [
            {
                "source": pubchem["source"],
                "source_identity": pubchem["source_identity"],
                "logical_index_sha256": pubchem["logical_index_sha256"],
            },
            {
                "source": coconut["source"],
                "source_identity": coconut["source_identity"],
                "logical_index_sha256": coconut["logical_index_sha256"],
            },
        ],
    }
    index_id = "cidx-" + sha256_bytes(
        json.dumps(identity_basis, sort_keys=True, separators=(",", ":")).encode()
    )[:24]

    receipt = {
        "receipt_version": "1.0.0",
        "state": "PASS",
        "candidate_index_id": index_id,
        "builder_version": BUILDER_VERSION,
        "mass_range_da": [MIN_MASS_DA, MAX_MASS_DA],
        "bin_width_da": BIN_WIDTH_DA,
        "index_semantics": {
            "retrieval_axis": "neutral monoisotopic/exact mass buckets",
            "formula_filter": "exact formula comparison inside selected mass buckets",
            "evaluator_identity_dedup": "query-time exact RDKit official-scorer identity; not precomputed over full source universe",
            "provenance_key": "source snapshot id + source record id",
            "production_rights_inferred": False,
        },
        "sources": [pubchem, coconut],
        "build_seconds_total": time.time() - started,
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "disk_usage_bytes": sum(
            p.stat().st_size for p in work.rglob("*") if p.is_file()
        ),
        "artifact_policy": {
            "index_payload_uploaded": False,
            "reason": "Rights-review-required derived data are not rehosted in the public worker artifact.",
            "rebuild_policy": "Reacquire frozen upstream bytes, require exact source SHA-256, rebuild, and require candidate_index_id match.",
        },
    }

    if (
        pubchem["readback_profile"]["state"] != "PASS"
        or coconut["readback_profile"]["state"] != "PASS"
    ):
        receipt["state"] = "FAIL"

    Path("results").mkdir(exist_ok=True)
    Path("results/candidate-index-build-receipt.json").write_text(
        json.dumps(receipt, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(receipt, indent=2, sort_keys=True))

    shutil.rmtree(work)
    return 0 if receipt["state"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
