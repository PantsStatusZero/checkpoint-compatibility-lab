from __future__ import annotations
import gzip, hashlib, json
from pathlib import Path
import requests
from rdkit import Chem, RDLogger, rdBase
from rdkit.Chem.MolStandardize import rdMolStandardize
RDLogger.DisableLog("rdApp.*")

SMILES_URL="https://ftp.ncbi.nlm.nih.gov/pubchem/Compound/Extras/CID-SMILES.gz"
INCHI_URL="https://ftp.ncbi.nlm.nih.gov/pubchem/Compound/Extras/CID-InChI-Key.gz"
EXPECTED_SMILES="abedf7b9709e98a1de9af5e3bea20d839c9953b5862157e6a6aafe55e15eb8c9"
EXPECTED_INCHI="265b61dcef948641cc79db8d1b96fdf6b55566dd8a9e5d758ab68fb24964fe65"
SAMPLE_MOD=1000

class HR:
    def __init__(self, raw):
        self.raw=raw; self.h=hashlib.sha256()
    def read(self,n=-1):
        b=self.raw.read(n)
        if b:self.h.update(b)
        return b
    def close(self): self.raw.close()

def stream(url):
    r=requests.get(url,stream=True,timeout=(30,1200));r.raise_for_status();r.raw.decode_content=False
    hr=HR(r.raw); gz=gzip.GzipFile(fileobj=hr,mode="rb")
    for line in gz:
        yield line
    gz.close()
    return hr.h.hexdigest()

def official(s):
    m=Chem.MolFromSmiles(s)
    if m is None:return None
    m=rdMolStandardize.TautomerEnumerator().Canonicalize(m)
    inc=Chem.MolToInchi(m)
    if not inc:return None
    return Chem.InchiToInchiKey(inc).split("-",1)[0]

# Download both compressed files to local ephemeral disk to permit coordinated streaming.
def dl(url,path,expected):
    h=hashlib.sha256()
    with requests.get(url,stream=True,timeout=(30,1200)) as r:
        r.raise_for_status()
        with path.open("wb") as f:
            for b in r.iter_content(4*1024*1024):
                if b:h.update(b);f.write(b)
    if h.hexdigest()!=expected:raise SystemExit(f"HASH_MISMATCH:{path.name}:{h.hexdigest()}")

work=Path("tmp-key-proxy");work.mkdir(exist_ok=True)
sp=work/"smiles.gz"; ip=work/"inchi.gz"
dl(SMILES_URL,sp,EXPECTED_SMILES);dl(INCHI_URL,ip,EXPECTED_INCHI)
checked=0;mismatches=[];parse_fail=0;rows=0
with gzip.open(sp,"rt",encoding="utf-8") as sf, gzip.open(ip,"rt",encoding="utf-8") as inf:
    sl=sf.readline(); il=inf.readline()
    while sl and il:
        s=sl.rstrip("\r\n").split("\t",1); q=il.rstrip("\r\n").split("\t")
        cs=int(s[0]); ci=int(q[0])
        if cs<ci: sl=sf.readline();continue
        if ci<cs: il=inf.readline();continue
        rows+=1
        if cs % SAMPLE_MOD == 0:
            checked+=1
            observed=official(s[1])
            stored=q[-1].split("-",1)[0]
            if observed is None: parse_fail+=1
            elif observed!=stored and len(mismatches)<100:
                mismatches.append({"cid":cs,"smiles":s[1],"stored":stored,"official":observed})
        sl=sf.readline();il=inf.readline()
Path("results").mkdir(exist_ok=True)
result={
 "state":"PASS" if not mismatches and parse_fail==0 else "MISMATCH_FOUND",
 "rdkit_version":rdBase.rdkitVersion,
 "sample_rule":f"CID % {SAMPLE_MOD} == 0",
 "joined_rows":rows,
 "checked":checked,
 "parse_failures":parse_fail,
 "mismatch_count_captured":len(mismatches),
 "mismatches":mismatches,
 "source_hashes":{"CID-SMILES.gz":EXPECTED_SMILES,"CID-InChI-Key.gz":EXPECTED_INCHI}
}
Path("results/pubchem-key14-proxy-validation.json").write_text(json.dumps(result,indent=2,sort_keys=True),encoding="utf-8")
print(json.dumps(result,indent=2,sort_keys=True))
