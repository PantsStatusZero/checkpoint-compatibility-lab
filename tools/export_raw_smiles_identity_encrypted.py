from __future__ import annotations
import base64,csv,gzip,hashlib,io,json,os,zipfile
from pathlib import Path
import requests, pyarrow.parquet as pq
from rdkit import Chem,RDLogger
from rdkit.Chem.MolStandardize import rdMolStandardize
from rdkit.Chem.Scaffolds import MurckoScaffold
from rdkit.Chem import Descriptors, rdMolDescriptors
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
RDLogger.DisableLog("rdApp.*")
URL="https://www.kaggle.com/api/v1/datasets/download/tyreejones393/casmi26-raw-index"
EXPECTED_ZIP="1427776966adc0e51c294ed8db3ec85ac3e115d711ccb3ef62fce39c31b57609"
EXPECTED_PARQUET="48c50a5394e8aab8e7ff67fd361174f77af1763cfacd288f77a754662af68330"
def sha(b):return hashlib.sha256(b).hexdigest()
r=requests.get(URL,timeout=300);r.raise_for_status();z=r.content
if sha(z)!=EXPECTED_ZIP:raise SystemExit("ZIP_HASH_MISMATCH")
with zipfile.ZipFile(io.BytesIO(z)) as zf:p=zf.read("casmi_raw_index.parquet")
if sha(p)!=EXPECTED_PARQUET:raise SystemExit("PARQUET_HASH_MISMATCH")
smiles=sorted(set(pq.ParquetFile(io.BytesIO(p)).read(columns=["smiles"]).column("smiles").to_pylist()))
out=io.BytesIO(); invalid=0
with gzip.GzipFile(fileobj=out,mode="wb",compresslevel=9,mtime=0) as gz:
  txt=io.TextIOWrapper(gz,encoding="utf-8",newline="",write_through=True); w=csv.writer(txt,lineterminator="\n")
  w.writerow(["smiles","key14","group_kind","group_value","formula","exact_mass"])
  te=rdMolStandardize.TautomerEnumerator()
  for i,s in enumerate(smiles,1):
    try:
      mol=Chem.MolFromSmiles(s)
      if mol is None: raise ValueError
      cm=te.Canonicalize(mol); inc=Chem.MolToInchi(cm); key=Chem.InchiToInchiKey(inc).split("-",1)[0]
      sc=MurckoScaffold.GetScaffoldForMol(mol)
      if sc.GetNumAtoms()==0: kind="ACYCLIC_EMPTY"; gv=key
      else: kind="SCAFFOLD"; gv=Chem.MolToSmiles(sc,canonical=True,isomericSmiles=False)
      w.writerow([s,key,kind,gv,rdMolDescriptors.CalcMolFormula(mol),f"{Descriptors.ExactMolWt(mol):.12f}"])
    except Exception:
      invalid+=1; w.writerow([s,"","INVALID","","",""])
    if i%20000==0: print("IDENTITY_PROGRESS",i,flush=True)
  txt.detach()
plain=out.getvalue()
pub=serialization.load_pem_public_key(Path("keys/candidate-index-materialization-public.pem").read_bytes())
der=pub.public_bytes(serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo); fp=sha(der)
key=os.urandom(32);nonce=os.urandom(12);enc=Cipher(algorithms.AES(key),modes.GCM(nonce)).encryptor();cipher=enc.update(plain)+enc.finalize()
wrapped=pub.encrypt(key,padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),algorithm=hashes.SHA256(),label=None))
Path("results").mkdir(exist_ok=True);Path("results/raw-smiles-identity.cipher").write_bytes(cipher)
manifest={"state":"PASS","rdkit_version":__import__("rdkit").rdBase.rdkitVersion,"source_zip_sha256":EXPECTED_ZIP,"source_parquet_sha256":EXPECTED_PARQUET,"unique_smiles":len(smiles),"invalid":invalid,"format":"csv.gz","plaintext_sha256":sha(plain),"plaintext_bytes":len(plain),"ciphertext_sha256":sha(cipher),"ciphertext_bytes":len(cipher),"public_key_fingerprint_sha256":fp,"encrypted_aes_key_b64":base64.b64encode(wrapped).decode(),"nonce_b64":base64.b64encode(nonce).decode(),"tag_b64":base64.b64encode(enc.tag).decode(),"plaintext_payload_uploaded":False}
Path("results/raw-smiles-identity-manifest.json").write_text(json.dumps(manifest,indent=2,sort_keys=True),encoding="utf-8")
print(json.dumps({k:v for k,v in manifest.items() if not k.endswith("_b64")},indent=2,sort_keys=True))
