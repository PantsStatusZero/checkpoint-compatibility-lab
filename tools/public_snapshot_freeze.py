from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import os
import platform
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import requests

CHUNK = 4 * 1024 * 1024

PUBCHEM_FILES = [
    (
        "CID-SMILES.gz",
        "https://ftp.ncbi.nlm.nih.gov/pubchem/Compound/Extras/CID-SMILES.gz",
    ),
    (
        "CID-InChI-Key.gz",
        "https://ftp.ncbi.nlm.nih.gov/pubchem/Compound/Extras/CID-InChI-Key.gz",
    ),
    (
        "CID-Mass.gz",
        "https://ftp.ncbi.nlm.nih.gov/pubchem/Compound/Extras/CID-Mass.gz",
    ),
]
COCONUT_URL = (
    "https://coconut.s3.uni-jena.de/prod/downloads/2026-09/"
    "coconut_csv-09-2026.zip"
)

EVIDENCE_URLS = {
    "pubchem_downloads": "https://pubchem.ncbi.nlm.nih.gov/docs/downloads",
    "ncbi_policies": "https://www.ncbi.nlm.nih.gov/home/about/policies/",
    "coconut_download": "https://coconut.naturalproducts.net/download",
    "coconut_terms": "https://coconut.naturalproducts.net/terms-of-service",
}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def fetch_evidence(session: requests.Session, url: str) -> dict:
    r = session.get(url, timeout=90, allow_redirects=True)
    data = r.content
    return {
        "requested_url": url,
        "final_url": r.url,
        "status_code": r.status_code,
        "content_type": r.headers.get("content-type"),
        "byte_length": len(data),
        "sha256": sha256_bytes(data),
        "etag": r.headers.get("etag"),
        "last_modified": r.headers.get("last-modified"),
        "retrieved_at": utcnow(),
    }


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
        try:
            self.raw.close()
        except Exception:
            pass


def stream_gzip_snapshot(session: requests.Session, name: str, url: str) -> dict:
    started = utcnow()
    r = session.get(url, stream=True, timeout=(30, 900), allow_redirects=True)
    r.raise_for_status()
    r.raw.decode_content = False

    wrapped = HashingReader(r.raw)
    decompressed_hash = hashlib.sha256()
    uncompressed_bytes = 0
    first_line = b""

    with gzip.GzipFile(fileobj=wrapped, mode="rb") as gz:
        while True:
            chunk = gz.read(CHUNK)
            if not chunk:
                break
            if not first_line:
                nl = chunk.find(b"\n")
                first_line = chunk if nl == -1 else chunk[: nl + 1]
            decompressed_hash.update(chunk)
            uncompressed_bytes += len(chunk)

    header_text = first_line.decode("utf-8", errors="replace").rstrip("\r\n")
    return {
        "name": name,
        "download_locator": url,
        "final_url": r.url,
        "retrieved_at_start": started,
        "retrieved_at_end": utcnow(),
        "http": {
            "status_code": r.status_code,
            "content_length": r.headers.get("content-length"),
            "etag": r.headers.get("etag"),
            "last_modified": r.headers.get("last-modified"),
            "content_type": r.headers.get("content-type"),
        },
        "archive_type": "gzip",
        "archive_sha256": wrapped.hash.hexdigest(),
        "byte_size": wrapped.byte_count,
        "extracted_content_sha256": decompressed_hash.hexdigest(),
        "uncompressed_bytes": uncompressed_bytes,
        "schema_detail": {
            "kind": "GZIP_FIRST_RECORD",
            "first_record": header_text[:1000],
            "sha256": sha256_bytes(first_line),
        },
    }


def download_coconut(session: requests.Session) -> dict:
    started = utcnow()
    h = hashlib.sha256()
    total = 0
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "coconut_csv-09-2026.zip"
        with session.get(
            COCONUT_URL, stream=True, timeout=(30, 900), allow_redirects=True
        ) as r:
            r.raise_for_status()
            headers = {
                "status_code": r.status_code,
                "content_length": r.headers.get("content-length"),
                "etag": r.headers.get("etag"),
                "last_modified": r.headers.get("last-modified"),
                "content_type": r.headers.get("content-type"),
            }
            with path.open("wb") as out:
                for chunk in r.iter_content(CHUNK):
                    if not chunk:
                        continue
                    h.update(chunk)
                    total += len(chunk)
                    out.write(chunk)

        seen = set()
        members = []
        extracted_digest = hashlib.sha256()
        csv_header = None
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                p = Path(info.filename)
                if (
                    not info.filename
                    or p.is_absolute()
                    or ".." in p.parts
                    or "\\" in info.filename
                ):
                    raise RuntimeError(f"unsafe ZIP member: {info.filename}")
                if info.filename in seen:
                    raise RuntimeError(f"duplicate ZIP member: {info.filename}")
                seen.add(info.filename)

                mh = hashlib.sha256()
                member_bytes = 0
                first_line = b""
                with zf.open(info, "r") as handle:
                    while True:
                        chunk = handle.read(CHUNK)
                        if not chunk:
                            break
                        if not first_line:
                            nl = chunk.find(b"\n")
                            first_line = chunk if nl == -1 else chunk[: nl + 1]
                        mh.update(chunk)
                        member_bytes += len(chunk)
                        extracted_digest.update(info.filename.encode("utf-8"))
                        extracted_digest.update(b"\0")
                        extracted_digest.update(chunk)
                if info.filename.lower().endswith(".csv") and csv_header is None:
                    text = first_line.decode("utf-8-sig", errors="strict")
                    header = next(csv.reader(io.StringIO(text)))
                    canonical = json.dumps(
                        header, separators=(",", ":"), ensure_ascii=False
                    ).encode("utf-8")
                    csv_header = {
                        "kind": "CSV_HEADER",
                        "columns": header,
                        "sha256": sha256_bytes(canonical),
                    }
                members.append(
                    {
                        "name": info.filename,
                        "compressed_bytes": info.compress_size,
                        "uncompressed_bytes": member_bytes,
                        "sha256": mh.hexdigest(),
                    }
                )

    return {
        "name": "coconut_csv-09-2026.zip",
        "download_locator": COCONUT_URL,
        "retrieved_at_start": started,
        "retrieved_at_end": utcnow(),
        "http": headers,
        "archive_type": "zip",
        "archive_sha256": h.hexdigest(),
        "byte_size": total,
        "extracted_content_sha256": extracted_digest.hexdigest(),
        "member_count": len(members),
        "members": members,
        "schema_detail": csv_header
        or {
            "kind": "ZIP_MEMBER_SET",
            "members": [m["name"] for m in members],
        },
    }


def main() -> int:
    mode = os.environ.get("SNAPSHOT_MODE", "all").lower()
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "public-source-snapshot/1.0 "
                "(reproducibility validation; contact via repository)"
            )
        }
    )

    evidence = {
        key: fetch_evidence(session, url) for key, url in EVIDENCE_URLS.items()
    }

    receipt = {
        "receipt_version": "1.0.0",
        "state": "RUNNING",
        "mode": mode,
        "retrieved_at": utcnow(),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "rights_evidence": evidence,
        "sources": {},
    }

    if mode in {"all", "coconut"}:
        receipt["sources"]["COCONUT"] = download_coconut(session)

    if mode in {"all", "pubchem"}:
        pubchem = []
        for name, url in PUBCHEM_FILES:
            pubchem.append(stream_gzip_snapshot(session, name, url))
        set_payload = json.dumps(
            [
                {
                    "name": item["name"],
                    "archive_sha256": item["archive_sha256"],
                    "extracted_content_sha256": item["extracted_content_sha256"],
                    "byte_size": item["byte_size"],
                }
                for item in pubchem
            ],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        receipt["sources"]["PUBCHEM"] = {
            "file_count": len(pubchem),
            "files": pubchem,
            "coordinated_fileset_sha256": sha256_bytes(set_payload),
        }

    required_evidence = {
        "pubchem_downloads",
        "ncbi_policies",
        "coconut_download",
        "coconut_terms",
    }
    evidence_ok = all(
        evidence[k]["status_code"] == 200 and evidence[k]["byte_length"] > 0
        for k in required_evidence
    )
    sources_ok = True
    if "COCONUT" in receipt["sources"]:
        c = receipt["sources"]["COCONUT"]
        sources_ok = sources_ok and bool(c["archive_sha256"]) and c["byte_size"] > 0
    if "PUBCHEM" in receipt["sources"]:
        sources_ok = sources_ok and all(
            f["archive_sha256"]
            and f["extracted_content_sha256"]
            and f["byte_size"] > 0
            for f in receipt["sources"]["PUBCHEM"]["files"]
        )

    receipt["checks"] = {
        "rights_evidence_retrieved": evidence_ok,
        "source_hashes_complete": sources_ok,
    }
    receipt["state"] = "PASS" if evidence_ok and sources_ok else "FAIL"
    receipt["completed_at"] = utcnow()

    Path("results").mkdir(exist_ok=True)
    out = Path(f"results/e01d-{mode}-snapshot-receipt.json")
    out.write_text(json.dumps(receipt, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt["state"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
