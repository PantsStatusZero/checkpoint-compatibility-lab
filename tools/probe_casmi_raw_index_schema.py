from __future__ import annotations
import hashlib, json, tempfile, zipfile
from pathlib import Path
import requests
import pyarrow.parquet as pq

URL="https://www.kaggle.com/api/v1/datasets/download/tyreejones393/casmi26-raw-index"
EXPECTED_ZIP="1427776966adc0e51c294ed8db3ec85ac3e115d711ccb3ef62fce39c31b57609"
EXPECTED_PARQUET="48c50a5394e8aab8e7ff67fd361174f77af1763cfacd288f77a754662af68330"

def sha(path):
    h=hashlib.sha256()
    with open(path,"rb") as f:
        for c in iter(lambda:f.read(4*1024*1024),b""): h.update(c)
    return h.hexdigest()

with tempfile.TemporaryDirectory() as td:
    zp=Path(td)/"raw-index.zip"
    with requests.get(URL,stream=True,timeout=(30,600),allow_redirects=True) as r:
        r.raise_for_status()
        with zp.open("wb") as out:
            for c in r.iter_content(4*1024*1024):
                if c: out.write(c)
    zhash=sha(zp)
    if zhash!=EXPECTED_ZIP: raise SystemExit(f"ZIP_HASH_MISMATCH:{zhash}")
    with zipfile.ZipFile(zp) as z:
        members=[{"name":i.filename,"compressed":i.compress_size,"uncompressed":i.file_size} for i in z.infolist() if not i.is_dir()]
        parquet_names=[i.filename for i in z.infolist() if i.filename.endswith(".parquet")]
        if len(parquet_names)!=1: raise SystemExit(f"PARQUET_COUNT:{parquet_names}")
        pp=Path(td)/"data.parquet"
        with z.open(parquet_names[0]) as src, pp.open("wb") as dst:
            while True:
                c=src.read(4*1024*1024)
                if not c: break
                dst.write(c)
    phash=sha(pp)
    if phash!=EXPECTED_PARQUET: raise SystemExit(f"PARQUET_HASH_MISMATCH:{phash}")
    pf=pq.ParquetFile(pp)
    schema=[{"name":f.name,"type":str(f.type)} for f in pf.schema_arrow]
    table=pf.read_row_group(0)
    cols=table.column_names
    preview={}
    for name in cols:
        arr=table[name].slice(0,3).to_pylist()
        preview[name]=arr
    print(json.dumps({
      "zip_sha256":zhash,
      "parquet_sha256":phash,
      "members":members,
      "num_rows":pf.metadata.num_rows,
      "num_row_groups":pf.metadata.num_row_groups,
      "schema":schema,
      "row_group0_rows":table.num_rows,
      "preview":preview
    },indent=2,sort_keys=True,default=str))
