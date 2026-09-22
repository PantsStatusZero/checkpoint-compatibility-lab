from __future__ import annotations
import base64, hashlib, io, json, os, zipfile
from pathlib import Path
import requests
import pyarrow.parquet as pq
import pyarrow as pa
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

URL="https://www.kaggle.com/api/v1/datasets/download/tyreejones393/casmi26-raw-index"
EXPECTED_ZIP="1427776966adc0e51c294ed8db3ec85ac3e115d711ccb3ef62fce39c31b57609"
EXPECTED_PARQUET="48c50a5394e8aab8e7ff67fd361174f77af1763cfacd288f77a754662af68330"

def sha(b): return hashlib.sha256(b).hexdigest()

r=requests.get(URL,timeout=300); r.raise_for_status(); z=r.content
if sha(z)!=EXPECTED_ZIP: raise SystemExit("ZIP_HASH_MISMATCH")
with zipfile.ZipFile(io.BytesIO(z)) as zf: p=zf.read("casmi_raw_index.parquet")
if sha(p)!=EXPECTED_PARQUET: raise SystemExit("PARQUET_HASH_MISMATCH")
pf=pq.ParquetFile(io.BytesIO(p))
table=pf.read(columns=["precursor_mz","smiles","adduct"])

payload=Path("raw-index-minimal.parquet")
pq.write_table(table,payload,compression="zstd",compression_level=9)
plain=payload.read_bytes()
plain_sha=sha(plain)

pub=serialization.load_pem_public_key(Path("keys/candidate-index-materialization-public.pem").read_bytes())
pub_der=pub.public_bytes(serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo)
fp=sha(pub_der)
key=os.urandom(32); nonce=os.urandom(12)
enc=Cipher(algorithms.AES(key),modes.GCM(nonce)).encryptor()
cipher=enc.update(plain)+enc.finalize()
wrapped=pub.encrypt(key,padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),algorithm=hashes.SHA256(),label=None))
Path("results").mkdir(exist_ok=True)
Path("results/raw-index-minimal.cipher").write_bytes(cipher)
manifest={
 "state":"PASS",
 "source_zip_sha256":EXPECTED_ZIP,
 "source_parquet_sha256":EXPECTED_PARQUET,
 "rows":table.num_rows,
 "columns":table.column_names,
 "plaintext_parquet_sha256":plain_sha,
 "plaintext_bytes":len(plain),
 "ciphertext_sha256":sha(cipher),
 "ciphertext_bytes":len(cipher),
 "public_key_fingerprint_sha256":fp,
 "encrypted_aes_key_b64":base64.b64encode(wrapped).decode(),
 "nonce_b64":base64.b64encode(nonce).decode(),
 "tag_b64":base64.b64encode(enc.tag).decode(),
 "transport":"RSA-OAEP-SHA256 + AES-256-GCM",
 "plaintext_payload_uploaded":False
}
Path("results/raw-index-minimal-manifest.json").write_text(json.dumps(manifest,indent=2,sort_keys=True),encoding="utf-8")
payload.unlink()
print(json.dumps({k:v for k,v in manifest.items() if not k.endswith("_b64")},indent=2,sort_keys=True))
