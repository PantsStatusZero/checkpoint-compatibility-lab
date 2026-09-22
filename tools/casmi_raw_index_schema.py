from __future__ import annotations
import hashlib, io, json, zipfile
from pathlib import Path
import requests
import pyarrow.parquet as pq

URL="https://www.kaggle.com/api/v1/datasets/download/tyreejones393/casmi26-raw-index"
EXPECTED_ZIP="1427776966adc0e51c294ed8db3ec85ac3e115d711ccb3ef62fce39c31b57609"
EXPECTED_PARQUET="48c50a5394e8aab8e7ff67fd361174f77af1763cfacd288f77a754662af68330"

def sha(b): return hashlib.sha256(b).hexdigest()

r=requests.get(URL, timeout=300)
r.raise_for_status()
z=r.content
if sha(z)!=EXPECTED_ZIP: raise SystemExit("zip hash mismatch")
with zipfile.ZipFile(io.BytesIO(z)) as zf:
    p=zf.read("casmi_raw_index.parquet")
if sha(p)!=EXPECTED_PARQUET: raise SystemExit("parquet hash mismatch")
pf=pq.ParquetFile(io.BytesIO(p))
schema=pf.schema_arrow
names=schema.names
wanted=["molecule_id","spectrum_id","smiles","adduct","ionization_mode","instrument_type","precursor_mz","collision_energy_ev","ms2_mzs","ms2_normalized_intensities","base_peak_intensity","molecular_formula","formula","exact_mass","monoisotopic_mass"]
present=[x for x in wanted if x in names]
summary={
  "zip_sha256":EXPECTED_ZIP,
  "parquet_sha256":EXPECTED_PARQUET,
  "rows":pf.metadata.num_rows,
  "row_groups":pf.metadata.num_row_groups,
  "columns":[{"name":f.name,"type":str(f.type)} for f in schema],
  "relevant_present":present,
}
# Small aggregate profile, no row values.
tbl=pf.read(columns=present)
for c in present:
    col=tbl.column(c)
    summary.setdefault("null_counts",{})[c]=col.null_count
Path("results").mkdir(exist_ok=True)
Path("results/raw-index-schema.json").write_text(json.dumps(summary,indent=2,sort_keys=True),encoding="utf-8")
print(json.dumps(summary,indent=2,sort_keys=True))
