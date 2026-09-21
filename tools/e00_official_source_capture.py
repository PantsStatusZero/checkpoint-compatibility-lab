from __future__ import annotations

import hashlib
import json
from pathlib import Path

import requests

URL = (
    "https://www.kaggle.com/api/v1/kernels/pull"
    "?userName=metric&kernelSlug=casmi-mean-reciprocal-rank"
)


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def describe(value):
    if isinstance(value, str):
        b = value.encode("utf-8")
        return {"type": "str", "length": len(b), "sha256": digest_bytes(b)}
    if isinstance(value, (dict, list)):
        stable = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return {
            "type": type(value).__name__,
            "length": len(stable),
            "canonical_sha256": digest_bytes(stable),
        }
    return {"type": type(value).__name__, "value": value}


def main() -> int:
    r = requests.get(URL, timeout=60)
    r.raise_for_status()
    payload = r.json()

    if not isinstance(payload, dict):
        raise SystemExit("unexpected Kaggle pull payload type")

    summary = {
        "http_status": r.status_code,
        "top_level_keys": sorted(payload),
        "fields": {k: describe(payload[k]) for k in sorted(payload)},
    }

    # Identify likely source-bearing fields without dumping the source.
    likely = {}
    for key, value in payload.items():
        if not isinstance(value, str):
            continue
        low = key.lower()
        if any(token in low for token in ("source", "script", "code", "blob", "kernel")):
            b = value.encode("utf-8")
            likely[key] = {
                "length": len(b),
                "sha256": digest_bytes(b),
                "contains_tautomer_enumerator": "TautomerEnumerator" in value,
                "contains_mol_from_smiles": "MolFromSmiles" in value,
                "contains_inchi_key": "Inchi" in value or "InChI" in value,
            }

    summary["likely_source_fields"] = likely
    Path("results").mkdir(exist_ok=True)
    Path("results/e00-kaggle-pull-structure.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
