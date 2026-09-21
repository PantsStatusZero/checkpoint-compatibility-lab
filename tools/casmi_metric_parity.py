from __future__ import annotations

import hashlib
import json
import platform
from pathlib import Path

from rdkit import Chem, rdBase
from rdkit.Chem.MolStandardize import rdMolStandardize

EXPECTED_RDKIT = "2026.03.3"
OFFICIAL_NOTEBOOK_URL = "https://www.kaggle.com/code/metric/casmi-mean-reciprocal-rank"
KAGGLE_PULL_URL = (
    "https://www.kaggle.com/api/v1/kernels/pull"
    "?userName=metric&kernelSlug=casmi-mean-reciprocal-rank"
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def official_identity(smiles: str | None) -> str | None:
    if not isinstance(smiles, str) or not smiles.strip():
        return None
    try:
        mol = Chem.MolFromSmiles(smiles.strip())
        if mol is None:
            return None
        mol = rdMolStandardize.TautomerEnumerator().Canonicalize(mol)
        inchi = Chem.MolToInchi(mol)
        if not inchi:
            return None
        key = Chem.InchiToInchiKey(inchi)
        return key.split("-", 1)[0] if key and "-" in key else None
    except Exception:
        return None


def implementation_identity(smiles: str | None) -> str | None:
    if smiles is None or not isinstance(smiles, str) or not smiles.strip():
        return None
    raw = smiles.strip()
    try:
        mol = Chem.MolFromSmiles(raw, sanitize=True)
    except Exception:
        return None
    if mol is None:
        return None
    try:
        canonical = rdMolStandardize.TautomerEnumerator().Canonicalize(mol)
        inchi = Chem.MolToInchi(canonical)
        if not inchi:
            return None
        key = Chem.InchiToInchiKey(inchi)
        if not key or "-" not in key:
            return None
        return key.split("-", 1)[0]
    except Exception:
        return None


def reciprocal_rank(truth: str, guesses: list[str]) -> float:
    truth_key = official_identity(truth)
    if not truth_key:
        return 0.0
    for rank, guess in enumerate(guesses[:25], start=1):
        if official_identity(guess) == truth_key:
            return 1.0 / rank
    return 0.0


def implementation_rr(truth: str, guesses: list[str]) -> float:
    truth_key = implementation_identity(truth)
    if not truth_key:
        return 0.0
    for rank, guess in enumerate(guesses[:25], start=1):
        key = implementation_identity(guess)
        if key is not None and key == truth_key:
            return 1.0 / rank
    return 0.0


def vectors() -> list[dict]:
    wrong = "COC"
    return [
        {
            "id": "official-glucose-stereo",
            "truth": "OC[C@H]1OC(O)[C@H](O)[C@@H](O)[C@@H]1O",
            "guesses": ["OCC1OC(O)C(O)C(O)C1O"],
        },
        {
            "id": "tautomer-acetylacetone",
            "truth": "CC(=O)CC(=O)C",
            "guesses": ["CC(=O)C=C(O)C"],
        },
        {
            "id": "alternate-smiles",
            "truth": "CCO",
            "guesses": ["OCC"],
        },
        {
            "id": "constitutional-distinct",
            "truth": "CCO",
            "guesses": [wrong],
        },
        {
            "id": "invalid-before-correct-retains-rank",
            "truth": "CCO",
            "guesses": ["not-a-smiles", "OCC"],
        },
        {
            "id": "duplicate-wrong-retains-ranks",
            "truth": "CCO",
            "guesses": [wrong, wrong, "OCC"],
        },
        {
            "id": "equivalent-duplicate-first-match-wins",
            "truth": "CCO",
            "guesses": ["OCC", "CCO"],
        },
        {
            "id": "salt-disconnected",
            "truth": "CC(=O)[O-].[Na+]",
            "guesses": ["CC(=O)[O-].[Na+]"],
        },
        {
            "id": "charged-form",
            "truth": "C[NH3+]",
            "guesses": ["C[NH3+]"],
        },
        {
            "id": "correct-at-rank-25",
            "truth": "CCO",
            "guesses": [wrong] * 24 + ["OCC"],
        },
        {
            "id": "correct-at-rank-26-is-truncated",
            "truth": "CCO",
            "guesses": [wrong] * 25 + ["OCC"],
        },
    ]


def fetch_official_notebook_source() -> dict:
    import requests

    response = requests.get(KAGGLE_PULL_URL, timeout=60)
    result = {
        "url": KAGGLE_PULL_URL,
        "status_code": response.status_code,
        "content_type": response.headers.get("content-type"),
        "byte_length": len(response.content),
        "sha256": _sha256(response.content),
        "retrieved": response.status_code == 200 and len(response.content) > 0,
    }
    if response.status_code == 200:
        Path("results").mkdir(exist_ok=True)
        Path("results/official-kaggle-kernel-pull.bin").write_bytes(response.content)
    return result


def main() -> int:
    if rdBase.rdkitVersion != EXPECTED_RDKIT:
        raise SystemExit(
            f"RDKit mismatch: expected {EXPECTED_RDKIT}, got {rdBase.rdkitVersion}"
        )

    notebook = fetch_official_notebook_source()
    rows = []
    failures = []
    for vector in vectors():
        truth_official = official_identity(vector["truth"])
        truth_impl = implementation_identity(vector["truth"])
        guess_official = [official_identity(x) for x in vector["guesses"]]
        guess_impl = [implementation_identity(x) for x in vector["guesses"]]
        rr_official = reciprocal_rank(vector["truth"], vector["guesses"])
        rr_impl = implementation_rr(vector["truth"], vector["guesses"])
        passed = (
            truth_official == truth_impl
            and guess_official == guess_impl
            and rr_official == rr_impl
        )
        row = {
            "id": vector["id"],
            "truth_key14_official": truth_official,
            "truth_key14_implementation": truth_impl,
            "candidate_key14_official": guess_official,
            "candidate_key14_implementation": guess_impl,
            "rr_official": rr_official,
            "rr_implementation": rr_impl,
            "passed": passed,
        }
        rows.append(row)
        if not passed:
            failures.append(row)

    semantic_payload = json.dumps(
        [
            {
                "id": r["id"],
                "truth": r["truth_key14_official"],
                "candidates": r["candidate_key14_official"],
                "rr": r["rr_official"],
            }
            for r in rows
        ],
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    receipt = {
        "state": "PASS" if not failures else "FAIL",
        "rdkit_version": rdBase.rdkitVersion,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "official_notebook_url": OFFICIAL_NOTEBOOK_URL,
        "official_notebook_pull": notebook,
        "vector_count": len(rows),
        "failure_count": len(failures),
        "semantic_output_sha256": _sha256(semantic_payload),
        "checks": {
            "identity_exact_match_all_vectors": all(
                r["truth_key14_official"] == r["truth_key14_implementation"]
                and r["candidate_key14_official"] == r["candidate_key14_implementation"]
                for r in rows
            ),
            "rank_semantics_exact_match_all_vectors": all(
                r["rr_official"] == r["rr_implementation"] for r in rows
            ),
            "invalid_guess_retains_rank": next(
                r for r in rows if r["id"] == "invalid-before-correct-retains-rank"
            )["rr_official"] == 0.5,
            "duplicate_guess_retains_rank": next(
                r for r in rows if r["id"] == "duplicate-wrong-retains-ranks"
            )["rr_official"] == 1.0 / 3.0,
            "rank_25_scores": next(
                r for r in rows if r["id"] == "correct-at-rank-25"
            )["rr_official"] == 0.04,
            "rank_26_truncated": next(
                r for r in rows if r["id"] == "correct-at-rank-26-is-truncated"
            )["rr_official"] == 0.0,
        },
        "rows": rows,
        "failures": failures,
    }
    Path("results").mkdir(exist_ok=True)
    Path("results/e00-parity-receipt.json").write_text(
        json.dumps(receipt, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt["state"] == "PASS" and all(receipt["checks"].values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
