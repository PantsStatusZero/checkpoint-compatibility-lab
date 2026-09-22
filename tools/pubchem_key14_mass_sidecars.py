from __future__ import annotations
import base64,gzip,hashlib,json,math,os,shutil,tarfile
from pathlib import Path
import requests
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes

SMILES_URL="https://ftp.ncbi.nlm.nih.gov/pubchem/Compound/Extras/CID-SMILES.gz"
MASS_URL="https://ftp.ncbi.nlm.nih.gov/pubchem/Compound/Extras/CID-Mass.gz"
INCHI_URL="https://ftp.ncbi.nlm.nih.gov/pubchem/Compound/Extras/CID-InChI-Key.gz"
EXPECTED={
 "CID-SMILES.gz":"abedf7b9709e98a1de9af5e3bea20d839c9953b5862157e6a6aafe55e15eb8c9",
 "CID-Mass.gz":"fd81bc739efb0d574df29188068ee9a9b29552d2d3167e71eda7984b9c6a545b",
 "CID-InChI-Key.gz":"265b61dcef948641cc79db8d1b96fdf6b55566dd8a9e5d758ab68fb24964fe65",
}
BIN=25;MAX=1500;CHUNK=4*1024*1024;ENVELOPE_CHUNK=400_000_000
class HR:
 def __init__(self,raw):self.raw=raw;self.h=hashlib.sha256();self.n=0
 def read(self,n=-1):
  b=self.raw.read(n)
  if b:self.h.update(b);self.n+=len(b)
  return b
 def close(self):self.raw.close()
class GS:
 def __init__(self,url):
  self.r=requests.get(url,stream=True,timeout=(30,1800));self.r.raise_for_status();self.r.raw.decode_content=False
  self.hr=HR(self.r.raw);self.gz=gzip.GzipFile(fileobj=self.hr,mode="rb")
 def next(self):
  b=self.gz.readline()
  if not b:return None
  return b.decode("utf-8").rstrip("\r\n").split("\t")
 def finish(self):
  for _ in iter(self.gz.readline,b""):pass
  self.gz.close();return {"sha256":self.hr.h.hexdigest(),"bytes":self.hr.n}
class W:
 def __init__(self,root):
  self.root=root;root.mkdir(exist_ok=True);self.hs={};self.gs={};self.st={}
 def write(self,mass,cid,key):
  b=int(math.floor(mass/BIN)*BIN)
  if b not in self.gs:
   p=self.root/f"mass-{b:04d}-{b+BIN:04d}-key14.tsv.gz";raw=p.open("wb");gz=gzip.GzipFile(filename="",fileobj=raw,mode="wb",compresslevel=1,mtime=0)
   self.gs[b]=(raw,gz,p);self.hs[b]=hashlib.sha256();self.st[b]={"rows":0}
  line=f"{cid}\t{key}\n".encode();self.gs[b][1].write(line);self.hs[b].update(line);self.st[b]["rows"]+=1
 def close(self):
  out=[]
  for b,(raw,gz,p) in self.gs.items():gz.close();raw.close()
  for b in sorted(self.gs):
   p=self.gs[b][2];h=hashlib.sha256()
   with p.open("rb") as f:
    for x in iter(lambda:f.read(CHUNK),b""):h.update(x)
   out.append({"bucket":b,"filename":p.name,"rows":self.st[b]["rows"],"logical_sha256":self.hs[b].hexdigest(),"compressed_sha256":h.hexdigest(),"compressed_bytes":p.stat().st_size})
  return out
def sha_file(p):
 h=hashlib.sha256()
 with p.open("rb") as f:
  for x in iter(lambda:f.read(CHUNK),b""):h.update(x)
 return h.hexdigest()

s=GS(SMILES_URL);m=GS(MASS_URL);i=GS(INCHI_URL);writer=W(Path("key14-sidecars"))
sr=s.next();mr=m.next();ir=i.next();joined=emitted=missing=0
while sr and mr and ir:
 ids=[int(sr[0]),int(mr[0]),int(ir[0])];mx=max(ids);mn=min(ids)
 if mn!=mx:
  if ids[0]==mn:sr=s.next()
  if ids[1]==mn:mr=m.next()
  if ids[2]==mn:ir=i.next()
  missing+=1;continue
 cid=ids[0];joined+=1
 if joined%10_000_000==0:print("KEY14_PROGRESS",joined,emitted,flush=True)
 mass=float(mr[2]);key=ir[-1].split("-",1)[0]
 if 0<=mass<=MAX:writer.write(mass,cid,key);emitted+=1
 sr=s.next();mr=m.next();ir=i.next()
meta={"CID-SMILES.gz":s.finish(),"CID-Mass.gz":m.finish(),"CID-InChI-Key.gz":i.finish()}
for n,v in meta.items():
 if v["sha256"]!=EXPECTED[n]:raise SystemExit(f"HASH_MISMATCH:{n}:{v['sha256']}")
shards=writer.close()
logical=hashlib.sha256(json.dumps([{"bucket":x["bucket"],"rows":x["rows"],"logical_sha256":x["logical_sha256"]} for x in shards],sort_keys=True,separators=(",",":")).encode()).hexdigest()

# Tar deterministic-ish payload; verify per-file hashes through manifest.
tar=Path("pubchem-key14-sidecars.tar")
with tarfile.open(tar,"w") as tf:
 for x in shards:tf.add(Path("key14-sidecars")/x["filename"],arcname=x["filename"])
pub=serialization.load_pem_public_key(Path("keys/candidate-index-materialization-public.pem").read_bytes())
der=pub.public_bytes(serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo);fp=hashlib.sha256(der).hexdigest()
key=os.urandom(32);nonce=os.urandom(12);enc=Cipher(algorithms.AES(key),modes.GCM(nonce)).encryptor()
Path("sidecar-envelope/chunks").mkdir(parents=True,exist_ok=True)
ch=[];agg=hashlib.sha256();plainh=hashlib.sha256();idx=0;buf=b"";totalc=totalp=0
def emit(data):
 global idx,buf,totalc
 buf+=data
 while len(buf)>=ENVELOPE_CHUNK:
  piece=buf[:ENVELOPE_CHUNK];buf=buf[ENVELOPE_CHUNK:];p=Path("sidecar-envelope/chunks")/f"cipher-{idx:03d}.bin";p.write_bytes(piece);ch.append({"name":p.name,"bytes":len(piece),"sha256":hashlib.sha256(piece).hexdigest()});idx+=1
with tar.open("rb") as f:
 for b in iter(lambda:f.read(CHUNK),b""):
  plainh.update(b);totalp+=len(b);c=enc.update(b);agg.update(c);totalc+=len(c);emit(c)
c=enc.finalize();agg.update(c);totalc+=len(c);emit(c)
if buf:
 p=Path("sidecar-envelope/chunks")/f"cipher-{idx:03d}.bin";p.write_bytes(buf);ch.append({"name":p.name,"bytes":len(buf),"sha256":hashlib.sha256(buf).hexdigest()})
wrapped=pub.encrypt(key,padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),algorithm=hashes.SHA256(),label=None))
manifest={"state":"PASS","sidecar_version":"PUBCHEM_KEY14_MASS_SIDECAR_v1","joined_records":joined,"indexed_records":emitted,"missing_alignment_events":missing,"bin_width_da":BIN,"max_mass_da":MAX,"shard_count":len(shards),"sidecar_logical_sha256":logical,"source_verification":meta,"shards":shards,"public_key_fingerprint_sha256":fp,"transport":"RSA-OAEP-SHA256 + AES-256-GCM","encrypted_aes_key_b64":base64.b64encode(wrapped).decode(),"nonce_b64":base64.b64encode(nonce).decode(),"tag_b64":base64.b64encode(enc.tag).decode(),"plaintext_tar_sha256":plainh.hexdigest(),"plaintext_tar_bytes":totalp,"ciphertext_sha256":agg.hexdigest(),"ciphertext_bytes":totalc,"chunks":ch,"plaintext_payload_uploaded":False}
Path("sidecar-envelope/manifest.json").write_text(json.dumps(manifest,indent=2,sort_keys=True),encoding="utf-8")
shutil.rmtree("key14-sidecars");tar.unlink()
print(json.dumps({k:v for k,v in manifest.items() if k not in ("shards","encrypted_aes_key_b64","nonce_b64","tag_b64")},indent=2,sort_keys=True))
