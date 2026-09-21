from __future__ import annotations

import argparse
import errno
import hashlib
import io
import json
import os
import pickletools
import re
import socket
import subprocess
import sys
import zipfile
from collections import Counter
from pathlib import Path

PREDICTOR = {
    "name": "fiddle_tcn_qtof.zip",
    "url": "https://github.com/josiehong/FIDDLE/releases/download/v2.0.0/fiddle_tcn_qtof.zip",
    "sha256": "08bf76055bece3e4d678a63423a8d01d67a933b1327e0baf4b97dc9e910fbaf4",
    "inner": "fiddle_tcn_qtof.pt",
}
RESCORER = {
    "name": "fiddle_rescore_qtof.zip",
    "url": "https://github.com/josiehong/FIDDLE/releases/download/v2.0.0/fiddle_rescore_qtof.zip",
    "sha256": "918aecc32fdafcc810eaa10cbd2abee555bd4c466ab45e7361cd659241cfafee",
    "inner": "fiddle_rescore_qtof.pt",
}
ASSETS = (PREDICTOR, RESCORER)

MAX_OUTER_MEMBER_BYTES = 700 * 1024 * 1024
MAX_INNER_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200.0
CREDENTIAL_NAME = re.compile(r"(TOKEN|SECRET|PASSWORD|CREDENTIAL|API_KEY|PRIVATE_KEY)", re.I)

APPROVED_PICKLE_GLOBALS = {
    "torch._utils._rebuild_tensor_v2",
    "torch.FloatStorage",
    "numpy.core.multiarray.scalar",
    "numpy.dtype",
    "collections.OrderedDict",
    "_codecs.encode",
}

EQUIVALENCE_FIXTURES = (
    {
        "fixture_id": "positive_adduct_1",
        "precursor_mass": 500.1234,
        "collision_energy": 20.0,
        "adduct_index": 1,
        "peak_indices": [211, 405, 792, 1280, 2211, 4004, 6112, 7201],
        "peak_intensities": [0.1, 0.4, 1.0, 0.2, 0.7, 0.3, 0.5, 0.15],
        "formulas": [
            [10, 14, 2, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0],
            [11, 16, 1, 4, 0, 0, 0, 0, 0, 0, 0, 0, 0],
            [9, 12, 3, 2, 0, 1, 0, 0, 0, 0, 0, 0, 0],
            [12, 18, 2, 2, 0, 0, 1, 0, 0, 0, 0, 0, 0],
        ],
        "require_score_tie": False,
    },
    {
        "fixture_id": "positive_adduct_2",
        "precursor_mass": 650.4321,
        "collision_energy": 35.0,
        "adduct_index": 2,
        "peak_indices": [101, 333, 901, 1702, 2900, 4555, 6800],
        "peak_intensities": [0.8, 0.15, 0.55, 1.0, 0.25, 0.45, 0.2],
        "formulas": [
            [14, 20, 2, 4, 0, 0, 0, 0, 0, 0, 0, 0, 0],
            [13, 18, 3, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0],
            [15, 22, 1, 5, 0, 1, 0, 0, 0, 0, 0, 0, 0],
        ],
        "require_score_tie": False,
    },
    {
        "fixture_id": "adduct_6_tie_case",
        "precursor_mass": 300.1111,
        "collision_energy": 10.0,
        "adduct_index": 6,
        "peak_indices": [77, 250, 777, 1555, 2600, 5100, 7000],
        "peak_intensities": [0.3, 0.9, 0.2, 0.6, 0.4, 1.0, 0.35],
        "formulas": [
            [8, 10, 2, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0],
            [8, 10, 2, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0],
            [9, 12, 1, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0],
        ],
        "require_score_tie": True,
    },
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_member_name(name: str) -> bool:
    p = Path(name)
    return bool(name) and not p.is_absolute() and ".." not in p.parts and "\\" not in name


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return ((info.external_attr >> 16) & 0o170000) == 0o120000


def _scan_zip_bytes(data: bytes, *, expected_inner: str | None = None) -> dict:
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise RuntimeError("QUALIFICATION_NOT_ZIP_ARCHIVE")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        infos = zf.infolist()
        for info in infos:
            if not _safe_member_name(info.filename):
                raise RuntimeError(f"QUALIFICATION_UNSAFE_ARCHIVE_PATH:{info.filename}")
            if _is_symlink(info):
                raise RuntimeError(f"QUALIFICATION_ARCHIVE_SYMLINK:{info.filename}")
            if info.file_size > MAX_OUTER_MEMBER_BYTES:
                raise RuntimeError(f"QUALIFICATION_MEMBER_TOO_LARGE:{info.filename}:{info.file_size}")
            if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
                raise RuntimeError(f"QUALIFICATION_COMPRESSION_RATIO:{info.filename}")
        files = [x.filename for x in infos if not x.is_dir()]
        if expected_inner is not None and files != [expected_inner]:
            raise RuntimeError(f"QUALIFICATION_OUTER_MEMBER_SET:{files}")
        return {
            "members": files,
            "member_count": len(files),
            "total_uncompressed_bytes": sum(x.file_size for x in infos),
        }


def _scan_torch_archive(path: Path) -> dict:
    if not zipfile.is_zipfile(path):
        raise RuntimeError(f"QUALIFICATION_TORCH_CHECKPOINT_NOT_ZIP:{path.name}")
    with zipfile.ZipFile(path) as zf:
        infos = zf.infolist()
        total = sum(x.file_size for x in infos)
        if total > MAX_INNER_TOTAL_BYTES:
            raise RuntimeError(f"QUALIFICATION_TORCH_ARCHIVE_TOO_LARGE:{total}")
        for info in infos:
            if not _safe_member_name(info.filename):
                raise RuntimeError(f"QUALIFICATION_UNSAFE_TORCH_PATH:{info.filename}")
            if _is_symlink(info):
                raise RuntimeError(f"QUALIFICATION_TORCH_ARCHIVE_SYMLINK:{info.filename}")
        data_pkl = [
            x.filename
            for x in infos
            if x.filename.endswith("/data.pkl") or x.filename == "data.pkl"
        ]
        if len(data_pkl) != 1:
            raise RuntimeError(f"QUALIFICATION_DATA_PKL_COUNT:{len(data_pkl)}")
        pickle_bytes = zf.read(data_pkl[0])
        op_counts = Counter()
        globals_seen = set()
        for op, arg, _pos in pickletools.genops(pickle_bytes):
            op_counts[op.name] += 1
            if op.name == "GLOBAL" and isinstance(arg, str):
                module, sep, name = arg.partition(" ")
                globals_seen.add(f"{module}.{name}" if sep else arg)
        forbidden = {"EXT1", "EXT2", "EXT4", "INST", "OBJ"}
        hit = sorted(forbidden.intersection(op_counts))
        if hit:
            raise RuntimeError(f"QUALIFICATION_FORBIDDEN_PICKLE_OPCODES:{hit}")
        unexpected_globals = sorted(globals_seen - APPROVED_PICKLE_GLOBALS)
        if unexpected_globals:
            raise RuntimeError(
                f"QUALIFICATION_UNREVIEWED_PICKLE_GLOBALS:{unexpected_globals}"
            )
        return {
            "archive_members": len(infos),
            "archive_total_uncompressed_bytes": total,
            "data_pkl_sha256": sha256_bytes(pickle_bytes),
            "pickle_opcode_counts": dict(sorted(op_counts.items())),
            "pickle_global_refs": sorted(globals_seen),
            "unexpected_pickle_global_refs": unexpected_globals,
        }


def fetch_static(work_dir: Path) -> dict:
    import requests

    work_dir.mkdir(parents=True, exist_ok=True)
    results = {}
    for asset in ASSETS:
        response = requests.get(asset["url"], timeout=300)
        response.raise_for_status()
        outer = response.content
        actual = sha256_bytes(outer)
        if actual != asset["sha256"]:
            raise RuntimeError(
                f"QUALIFICATION_OUTER_HASH_MISMATCH:{asset['name']}:{actual}"
            )
        outer_scan = _scan_zip_bytes(outer, expected_inner=asset["inner"])
        with zipfile.ZipFile(io.BytesIO(outer)) as zf:
            inner = zf.read(asset["inner"])
        inner_path = work_dir / asset["inner"]
        inner_path.write_bytes(inner)
        results[asset["name"]] = {
            "expected_sha256": asset["sha256"],
            "actual_sha256": actual,
            "outer_scan": outer_scan,
            "inner_name": asset["inner"],
            "inner_sha256": sha256_bytes(inner),
            "inner_bytes": len(inner),
            "torch_archive_static_scan": _scan_torch_archive(inner_path),
        }
    result = {"phase": "FETCH_STATIC_PASS", "assets": results}
    (work_dir / "static-scan.json").write_text(
        json.dumps(result, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print("STATIC_QUALIFICATION_PASS")
    return result


def _proc_status_value(name: str) -> str | None:
    path = Path("/proc/self/status")
    if not path.exists():
        return None
    prefix = f"{name}:"
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(prefix):
            return line.split(":", 1)[1].strip()
    return None


def _verify_os_isolation() -> dict:
    cap_eff_raw = _proc_status_value("CapEff")
    no_new_privs_raw = _proc_status_value("NoNewPrivs")
    if cap_eff_raw is None or int(cap_eff_raw, 16) != 0:
        raise RuntimeError(f"QUALIFICATION_LINUX_CAPABILITIES_NOT_DROPPED:{cap_eff_raw}")
    if no_new_privs_raw != "1":
        raise RuntimeError(
            f"QUALIFICATION_NO_NEW_PRIVILEGES_NOT_SET:{no_new_privs_raw}"
        )

    probe = Path("/qualification-rootfs-write-probe")
    rootfs_read_only = False
    try:
        probe.write_text("should-not-write", encoding="utf-8")
    except OSError as exc:
        rootfs_read_only = exc.errno in {errno.EROFS, errno.EACCES, errno.EPERM}
    else:
        probe.unlink(missing_ok=True)
    if not rootfs_read_only:
        raise RuntimeError("QUALIFICATION_ROOT_FILESYSTEM_NOT_READ_ONLY")

    non_loopback_routes = []
    route_path = Path("/proc/net/route")
    if route_path.exists():
        for line in route_path.read_text(encoding="utf-8").splitlines()[1:]:
            cols = line.split()
            if cols and cols[0] != "lo":
                non_loopback_routes.append(line)
    if non_loopback_routes:
        raise RuntimeError(
            f"QUALIFICATION_NON_LOOPBACK_ROUTE_PRESENT:{non_loopback_routes}"
        )

    return {
        "linux_cap_eff": cap_eff_raw,
        "no_new_privileges": True,
        "read_only_root_filesystem": True,
        "non_loopback_route_count": 0,
    }


def _environment_manifest() -> dict:
    required = (
        "QUALIFICATION_GIT_COMMIT",
        "QUALIFICATION_CONTAINER_IMAGE_ID",
        "QUALIFICATION_CONTAINER_DEFINITION_SHA256",
        "QUALIFICATION_DEPENDENCY_INVENTORY_SHA256",
    )
    values = {}
    missing = []
    for name in required:
        value = os.environ.get(name)
        if not value:
            missing.append(name)
        else:
            values[name] = value
    if missing:
        raise RuntimeError(f"QUALIFICATION_ENVIRONMENT_MANIFEST_MISSING:{missing}")
    basis = {
        "git_commit": values["QUALIFICATION_GIT_COMMIT"],
        "container_image_id": values["QUALIFICATION_CONTAINER_IMAGE_ID"],
        "container_definition_sha256": values[
            "QUALIFICATION_CONTAINER_DEFINITION_SHA256"
        ],
        "dependency_inventory_sha256": values[
            "QUALIFICATION_DEPENDENCY_INVENTORY_SHA256"
        ],
    }
    basis["environment_manifest_sha256"] = hashlib.sha256(
        json.dumps(basis, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return basis


def _install_runtime_guards() -> dict:
    credential_vars = sorted(k for k in os.environ if CREDENTIAL_NAME.search(k))
    if credential_vars:
        raise RuntimeError(f"QUALIFICATION_CREDENTIAL_ENV_PRESENT:{credential_vars}")

    def blocked(*_args, **_kwargs):
        raise RuntimeError("QUALIFICATION_NETWORK_OR_SUBPROCESS_BLOCKED")

    socket.create_connection = blocked
    socket.getaddrinfo = blocked
    socket.socket.connect = blocked
    subprocess.Popen = blocked
    os.system = blocked

    network_guard_pass = False
    try:
        socket.create_connection(("example.com", 443), timeout=0.1)
    except RuntimeError as exc:
        network_guard_pass = str(exc) == "QUALIFICATION_NETWORK_OR_SUBPROCESS_BLOCKED"
    if not network_guard_pass:
        raise RuntimeError("QUALIFICATION_NETWORK_GUARD_FAILED")

    return {
        "credential_env_count": 0,
        "network_guard_pass": True,
        "subprocess_guard_installed": True,
    }


def _tensor_digest(state: dict) -> str:
    import torch

    h = hashlib.sha256()
    for key in sorted(state):
        value = state[key]
        if not isinstance(value, torch.Tensor):
            raise RuntimeError(
                f"QUALIFICATION_NON_TENSOR_STATE_VALUE:{key}:{type(value).__name__}"
            )
        tensor = value.detach().cpu().contiguous()
        raw = tensor.view(torch.uint8).numpy().tobytes()
        h.update(key.encode())
        h.update(str(tensor.dtype).encode())
        h.update(json.dumps(list(tensor.shape)).encode())
        h.update(hashlib.sha256(raw).digest())
    return h.hexdigest()


def _normalize_model_state(state: dict) -> dict:
    return {k[7:] if k.startswith("module.") else k: v for k, v in state.items()}


def _safe_load(path: Path):
    import _codecs
    import collections

    import numpy as np
    import torch

    if not hasattr(torch.serialization, "get_unsafe_globals_in_checkpoint"):
        raise RuntimeError("QUALIFICATION_TORCH_UNSAFE_GLOBAL_SCANNER_UNAVAILABLE")

    scanner_globals = sorted(torch.serialization.get_unsafe_globals_in_checkpoint(path))
    unexpected = sorted(set(scanner_globals) - APPROVED_PICKLE_GLOBALS)
    if unexpected:
        raise RuntimeError(
            f"QUALIFICATION_UNREVIEWED_TORCH_GLOBALS:{path.name}:{unexpected}"
        )

    reviewed_objects = {
        "torch._utils._rebuild_tensor_v2": torch._utils._rebuild_tensor_v2,
        "torch.FloatStorage": torch.FloatStorage,
        "numpy.core.multiarray.scalar": np.core.multiarray.scalar,
        "numpy.dtype": np.dtype,
        "collections.OrderedDict": collections.OrderedDict,
        "_codecs.encode": _codecs.encode,
    }
    safe_objects = [reviewed_objects[name] for name in scanner_globals]

    # The actual v2.0.0 checkpoints reconstruct NumPy float dtype classes
    # dynamically through numpy.dtype. Review and allow only the observed
    # float32/float64 dtype classes rather than all NumPy dtype types.
    for scalar_type in (np.float32, np.float64):
        dtype_class = type(np.dtype(scalar_type))
        if dtype_class not in safe_objects:
            safe_objects.append(dtype_class)

    runtime_safe_global_types = sorted(
        {
            f"{obj.__module__}.{obj.__qualname__}"
            for obj in safe_objects
            if hasattr(obj, "__module__") and hasattr(obj, "__qualname__")
        }
    )

    with torch.serialization.safe_globals(safe_objects):
        payload = torch.load(path, map_location="cpu", weights_only=True)

    return payload, {
        "scanner_globals": scanner_globals,
        "approved_globals": sorted(set(scanner_globals)),
        "runtime_safe_global_types": runtime_safe_global_types,
        "unexpected_globals": unexpected,
    }


def safe_reproduce(work_dir: Path, out_path: Path) -> dict:
    os_isolation = _verify_os_isolation()
    environment_manifest = _environment_manifest()
    guards = _install_runtime_guards()

    import safetensors
    import torch
    import torch.nn.functional as F
    from safetensors.torch import load_file, save_file

    from .fiddle_model import FormulaEncoder, MS2FNetTCN, RescoreHead

    predictor_path = work_dir / PREDICTOR["inner"]
    rescorer_path = work_dir / RESCORER["inner"]
    predictor_ckpt, predictor_scan = _safe_load(predictor_path)
    rescorer_ckpt, rescorer_scan = _safe_load(rescorer_path)

    if "model_state_dict" not in predictor_ckpt:
        raise RuntimeError("QUALIFICATION_PREDICTOR_STATE_DICT_MISSING")
    if "formula_encoder_state_dict" not in rescorer_ckpt:
        raise RuntimeError("QUALIFICATION_FORMULA_ENCODER_STATE_DICT_MISSING")
    if "rescore_head_state_dict" not in rescorer_ckpt:
        raise RuntimeError("QUALIFICATION_RESCORE_HEAD_STATE_DICT_MISSING")

    predictor_state = _normalize_model_state(predictor_ckpt["model_state_dict"])
    formula_state = rescorer_ckpt["formula_encoder_state_dict"]
    head_state = rescorer_ckpt["rescore_head_state_dict"]

    source_digests = {
        "predictor": _tensor_digest(predictor_state),
        "formula_encoder": _tensor_digest(formula_state),
        "rescore_head": _tensor_digest(head_state),
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    converted_dir = out_path.parent / "converted"
    converted_dir.mkdir(parents=True, exist_ok=True)

    predictor_safe = converted_dir / "fiddle_tcn_qtof.safetensors"
    formula_safe = converted_dir / "fiddle_rescore_formula_encoder_qtof.safetensors"
    head_safe = converted_dir / "fiddle_rescore_head_qtof.safetensors"

    # The reviewed model topology intentionally exposes duplicate state-dict
    # keys for modules referenced both directly and through Sequential
    # containers. Clone each tensor before serialization so safetensors stores
    # key-complete values without retaining Python/PyTorch storage aliasing.
    # Exact key/value digest and output/rank equivalence below must still match.

    save_file(
        {k: v.detach().cpu().contiguous().clone() for k, v in predictor_state.items()},
        predictor_safe,
    )
    save_file(
        {k: v.detach().cpu().contiguous().clone() for k, v in formula_state.items()},
        formula_safe,
    )
    save_file(
        {k: v.detach().cpu().contiguous().clone() for k, v in head_state.items()},
        head_safe,
    )

    predictor_reloaded = load_file(predictor_safe, device="cpu")
    formula_reloaded = load_file(formula_safe, device="cpu")
    head_reloaded = load_file(head_safe, device="cpu")

    converted_digests = {
        "predictor": _tensor_digest(predictor_reloaded),
        "formula_encoder": _tensor_digest(formula_reloaded),
        "rescore_head": _tensor_digest(head_reloaded),
    }
    if source_digests != converted_digests:
        raise RuntimeError("QUALIFICATION_TENSOR_DIGEST_MISMATCH_AFTER_SAFETENSORS")

    torch.manual_seed(20260920)
    original_model = MS2FNetTCN()
    converted_model = MS2FNetTCN()
    original_model.load_state_dict(predictor_state, strict=True)
    converted_model.load_state_dict(predictor_reloaded, strict=True)
    original_model.eval()
    converted_model.eval()

    original_formula = FormulaEncoder()
    converted_formula = FormulaEncoder()
    original_head = RescoreHead()
    converted_head = RescoreHead()
    original_formula.load_state_dict(formula_state, strict=True)
    converted_formula.load_state_dict(formula_reloaded, strict=True)
    original_head.load_state_dict(head_state, strict=True)
    converted_head.load_state_dict(head_reloaded, strict=True)
    for module in (original_formula, converted_formula, original_head, converted_head):
        module.eval()

    fixture_results = []
    predictor_max_abs = 0.0
    rescore_max_abs = 0.0

    for fixture in EQUIVALENCE_FIXTURES:
        x = torch.zeros((1, 7520), dtype=torch.float32)
        peak_idx = torch.tensor(fixture["peak_indices"])
        x[0, peak_idx] = torch.tensor(
            fixture["peak_intensities"], dtype=torch.float32
        )
        env = torch.tensor(
            [[
                fixture["precursor_mass"],
                fixture["collision_energy"],
                float(fixture["adduct_index"]),
            ]],
            dtype=torch.float32,
        )
        formulas = torch.tensor(fixture["formulas"], dtype=torch.float32)

        with torch.no_grad():
            original_outputs = original_model(x, env)
            converted_outputs = converted_model(x, env)

        fixture_predictor_max = 0.0
        for a, b in zip(original_outputs, converted_outputs):
            diff = float(torch.max(torch.abs(a - b)).item())
            fixture_predictor_max = max(fixture_predictor_max, diff)
            predictor_max_abs = max(predictor_max_abs, diff)
            torch.testing.assert_close(a, b, rtol=0.0, atol=0.0)

        with torch.no_grad():
            z_orig = F.normalize(original_outputs[0], dim=1).expand(
                len(formulas), -1
            )
            z_conv = F.normalize(converted_outputs[0], dim=1).expand(
                len(formulas), -1
            )
            scores_orig = torch.sigmoid(
                original_head(z_orig * original_formula(formulas))
            )
            scores_conv = torch.sigmoid(
                converted_head(z_conv * converted_formula(formulas))
            )

        fixture_rescore_max = float(
            torch.max(torch.abs(scores_orig - scores_conv)).item()
        )
        rescore_max_abs = max(rescore_max_abs, fixture_rescore_max)
        torch.testing.assert_close(scores_orig, scores_conv, rtol=0.0, atol=0.0)

        rank_orig = torch.argsort(scores_orig, descending=True, stable=True).tolist()
        rank_conv = torch.argsort(scores_conv, descending=True, stable=True).tolist()
        if rank_orig != rank_conv:
            raise RuntimeError(
                f"QUALIFICATION_RANK_MISMATCH:{fixture['fixture_id']}:"
                f"{rank_orig}:{rank_conv}"
            )

        tie_verified = None
        if fixture["require_score_tie"]:
            tie_verified = bool(scores_orig[0].item() == scores_orig[1].item())
            if not tie_verified:
                raise RuntimeError(
                    f"QUALIFICATION_EXPECTED_TIE_NOT_OBSERVED:{fixture['fixture_id']}"
                )

        fixture_results.append(
            {
                "fixture_id": fixture["fixture_id"],
                "adduct_index": fixture["adduct_index"],
                "predictor_max_abs_diff": fixture_predictor_max,
                "rescore_max_abs_diff": fixture_rescore_max,
                "rank_original": rank_orig,
                "rank_converted": rank_conv,
                "tie_required": fixture["require_score_tie"],
                "tie_verified": tie_verified,
            }
        )

    safe_assets = {
        "predictor": {
            "name": predictor_safe.name,
            "sha256": sha256_file(predictor_safe),
            "bytes": predictor_safe.stat().st_size,
        },
        "formula_encoder": {
            "name": formula_safe.name,
            "sha256": sha256_file(formula_safe),
            "bytes": formula_safe.stat().st_size,
        },
        "rescore_head": {
            "name": head_safe.name,
            "sha256": sha256_file(head_safe),
            "bytes": head_safe.stat().st_size,
        },
    }

    static_scan_path = work_dir / "static-scan.json"
    if not static_scan_path.exists():
        raise RuntimeError("QUALIFICATION_STATIC_SCAN_RECEIPT_MISSING")
    static_scan = json.loads(static_scan_path.read_text(encoding="utf-8"))

    outer_hashes = {}
    for asset in ASSETS:
        row = static_scan["assets"].get(asset["name"])
        if row is None or row["actual_sha256"] != asset["sha256"]:
            raise RuntimeError(
                f"QUALIFICATION_STATIC_SCAN_HASH_PROOF_MISSING:{asset['name']}"
            )
        outer_hashes[asset["name"]] = row["actual_sha256"]

    environment_manifest_path = out_path.parent / "environment-manifest.json"
    environment_manifest_path.write_text(
        json.dumps(environment_manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    receipt_basis = {
        "source_outer_sha256": outer_hashes,
        "source_inner_sha256": {
            "predictor": sha256_file(predictor_path),
            "rescorer": sha256_file(rescorer_path),
        },
        "static_scan_sha256": sha256_file(static_scan_path),
        "tensor_digests": source_digests,
        "safe_assets": safe_assets,
        "fixture_results": fixture_results,
        "predictor_max_abs_diff": predictor_max_abs,
        "rescore_max_abs_diff": rescore_max_abs,
        "environment_manifest": environment_manifest,
        "torch_version": torch.__version__,
        "safetensors_version": safetensors.__version__,
    }
    receipt_sha = hashlib.sha256(
        json.dumps(receipt_basis, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    result = {
        "qualification_id": "FIDDLE-V2-QTOF-CHECKPOINT-COMPATIBILITY",
        "state": "PASS",
        "security_state": "SECURITY_REPRODUCTION_PASS",
        "original_checkpoint_policy": "QUARANTINED_NO_DIRECT_RUNTIME_DESERIALIZATION",
        "runtime_checkpoint_policy": "SAFETENSORS_ONLY",
        "isolation": {**guards, **os_isolation},
        "environment_manifest": environment_manifest,
        "source_outer_sha256": outer_hashes,
        "static_scan_sha256": receipt_basis["static_scan_sha256"],
        "checkpoint_global_scan": {
            "predictor": predictor_scan,
            "rescorer": rescorer_scan,
        },
        "source_inner_sha256": receipt_basis["source_inner_sha256"],
        "top_level_checkpoint_keys": {
            "predictor": sorted(predictor_ckpt.keys()),
            "rescorer": sorted(rescorer_ckpt.keys()),
        },
        "tensor_digests": source_digests,
        "safe_runtime_assets": safe_assets,
        "equivalence": {
            "fixture_count": len(fixture_results),
            "fixtures": fixture_results,
            "predictor_fixed_input_max_abs_diff": predictor_max_abs,
            "rescore_fixed_input_max_abs_diff": rescore_max_abs,
            "exact_tensor_digest_match": True,
            "all_fixture_equivalence": True,
            "tie_fixture_verified": any(
                row["tie_required"] and row["tie_verified"] is True
                for row in fixture_results
            ),
        },
        "versions": {
            "python": sys.version.split()[0],
            "torch": torch.__version__,
            "safetensors": safetensors.__version__,
        },
        "receipt_sha256": receipt_sha,
    }

    out_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print("QUALIFICATION_RESULT_BEGIN")
    print(json.dumps(result, indent=2, sort_keys=True))
    print("QUALIFICATION_RESULT_END")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("fetch-static", "safe-reproduce"), required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    if args.phase == "fetch-static":
        fetch_static(args.work_dir)
        return 0

    if args.out is None:
        raise SystemExit("--out is required for safe-reproduce")

    result = safe_reproduce(args.work_dir, args.out)
    return 0 if result["state"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
