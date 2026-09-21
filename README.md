# Checkpoint Compatibility Lab

Public, bounded software-security and checkpoint-compatibility qualification tooling.

This repository validates selected public model checkpoints against a frozen security and exact-equivalence contract. It is intentionally limited to software development, compatibility testing, and release qualification. It is not a general-purpose compute service.

## Current qualification target

- Upstream project: FIDDLE
- Release: v2.0.0
- Model family: QTOF predictor + rescorer
- Runtime: PyTorch 2.10.0 CPU
- Safe runtime format: safetensors

The workflow verifies published release hashes, rejects unsafe archive structure, statically inspects checkpoint serialization, loads only through the reviewed `weights_only=True` path, converts inference tensors to safetensors, and requires exact tensor/output/rank equivalence across multiple deterministic fixtures.

## Public disclosure boundary

Everything in this repository, its workflow metadata, logs, and qualification receipts is public.

Do not add private project names, customer/research context, internal ticket IDs, private repository names, legal conclusions, competition strategy, credentials, or other private metadata.

## Security posture

The safe-load phase runs with:

- no network;
- all Linux capabilities dropped;
- `no-new-privileges`;
- read-only root filesystem;
- credential-free environment;
- explicit dependency/environment hashing.

A compatibility PASS is a technical security/reproducibility result only. It does not establish licensing, data-rights, or downstream-use permission.

## License

Apache-2.0. See `LICENSE`.
