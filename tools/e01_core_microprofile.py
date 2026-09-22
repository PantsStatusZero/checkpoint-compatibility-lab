from __future__ import annotations

import gzip
import importlib.util
import json
import math
import os
import resource
import shutil
import sys
import time
from pathlib import Path

EXPECTED_INDEX_ID="cidx-b2cd590c630213d6648665bd"
EXPECTED_BUILDER="PUBLIC-CANDIDATE-MASS-INDEX-v1.6"
EXPECTED_LOGICAL={
    "PUBCHEM":"79823ce75f9d9815ec0ca77c91532567b5601115b342e3ae745786e7a555133c",
    "COCONUT":"9ab38206dd6580331b5d8b71f5db549fe97d8f51a060d4d86329c545ed421ad2",
}
TARGETS=[157,200,250,300,348,400,500,600,750,900,1050,1159]
WINDOW=0.01
BIN_WIDTH=25

def load_builder():
    path=Path("tools/public_candidate_index_build.py")
    spec=importlib.util.spec_from_file_location("candidate_index_builder",path)
    module=importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name]=module
    spec.loader.exec_module(module)
    return module

def parse_line(source,line):
    parts=line.rstrip("\r\n").split("\t")
    if source=="PUBCHEM":
        if len(parts)!=4: raise ValueError(len(parts))
        return float(parts[1]), parts[2], parts
    if source=="COCONUT":
        if len(parts)!=7: raise ValueError(len(parts))
        return float(parts[1]), parts[2], parts
    raise ValueError(source)

def bucket_start(mass):
    return int(math.floor(mass/BIN_WIDTH)*BIN_WIDTH)

def shard_path(root,source,mass):
    b=bucket_start(mass)
    return root/source.lower()/f"mass-{b:04d}-{b+BIN_WIDTH:04d}.tsv.gz"

def query(root,source,target,formula=None):
    path=shard_path(root,source,target)
    lo,hi=target-WINDOW,target+WINDOW
    t=time.perf_counter()
    count=0
    sample_formula=None
    rows=0
    with gzip.open(path,"rt",encoding="utf-8",newline="") as f:
        for line in f:
            rows+=1
            mass,frm,_=parse_line(source,line)
            if lo <= mass <= hi:
                if sample_formula is None:
                    sample_formula=frm
                if formula is None or frm==formula:
                    count+=1
    dt=time.perf_counter()-t
    return {"seconds":dt,"candidate_count":count,"rows_scanned":rows,"sample_formula":sample_formula,"path":path.name}

def full_scan(root,source,filename):
    path=root/source.lower()/filename
    t=time.perf_counter()
    rows=0
    with gzip.open(path,"rb") as f:
        for _ in f: rows+=1
    dt=time.perf_counter()-t
    return {"source":source,"filename":filename,"rows":rows,"seconds":dt,"rows_per_second":rows/dt if dt else None}

builder=load_builder()
if builder.BUILDER_VERSION!=EXPECTED_BUILDER:
    raise SystemExit(f"BUILDER_VERSION_MISMATCH:{builder.BUILDER_VERSION}")

orig_rmtree=builder.shutil.rmtree
def preserve(path,*args,**kwargs):
    if Path(path).name=="candidate-index-work":
        return
    return orig_rmtree(path,*args,**kwargs)
builder.shutil.rmtree=preserve
start=time.perf_counter()
rc=builder.main()
build_seconds=time.perf_counter()-start
builder.shutil.rmtree=orig_rmtree
if rc!=0: raise SystemExit(rc)

receipt=json.loads(Path("results/candidate-index-build-receipt.json").read_text())
if receipt["candidate_index_id"]!=EXPECTED_INDEX_ID: raise SystemExit("INDEX_ID_MISMATCH")
observed={x["source"]:x["logical_index_sha256"] for x in receipt["sources"]}
if observed!=EXPECTED_LOGICAL: raise SystemExit(f"LOGICAL_HASH_MISMATCH:{observed}")
root=Path("candidate-index-work")

meta_t=time.perf_counter()
files=list(root.rglob("*.tsv.gz"))
metadata_scan_seconds=time.perf_counter()-meta_t
disk_bytes=sum(p.stat().st_size for p in files)

queries=[]
for source in ("PUBCHEM","COCONUT"):
    for target in TARGETS:
        cold=query(root,source,target)
        warm=query(root,source,target)
        formula=None
        formula_result=None
        if cold["sample_formula"]:
            formula=cold["sample_formula"]
            formula_result=query(root,source,target,formula=formula)
        queries.append({
            "source":source,"target_mass_da":target,
            "cold":cold,"warm":warm,
            "formula":formula,
            "formula_filtered":formula_result,
        })

# Explicitly rescan the accepted largest-row shard from each source receipt.
largest={}
for row in receipt["sources"]:
    source=row["source"]
    shard=max(row["shards"],key=lambda x:x["row_count"])
    largest[source]=full_scan(root,source,shard["filename"])

rss_kb=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
summary={
  "state":"PASS",
  "profile_version":"E01-CORE-MICROPROFILE-v1",
  "candidate_index_id":EXPECTED_INDEX_ID,
  "builder_version":EXPECTED_BUILDER,
  "source_logical_index_sha256":observed,
  "build_seconds":build_seconds,
  "index_disk_bytes":disk_bytes,
  "shard_file_count":len(files),
  "metadata_scan_seconds":metadata_scan_seconds,
  "query_window_da":WINDOW,
  "fixed_targets_da":TARGETS,
  "queries":queries,
  "largest_shard_scans":largest,
  "peak_rss_bytes":int(rss_kb*1024),
  "environment":{
      "platform":receipt.get("platform"),
      "python_version":receipt.get("python_version"),
      "scope":"EARLY_ENGINEERING_MICROPROFILE_NOT_FINAL_KAGGLE_RUNTIME"
  }
}
cold=[q["cold"]["seconds"] for q in queries]
warm=[q["warm"]["seconds"] for q in queries]
counts=[q["cold"]["candidate_count"] for q in queries]
formula_times=[q["formula_filtered"]["seconds"] for q in queries if q["formula_filtered"]]
formula_counts=[q["formula_filtered"]["candidate_count"] for q in queries if q["formula_filtered"]]
summary["aggregates"]={
  "cold_query_seconds":{"min":min(cold),"median":sorted(cold)[len(cold)//2],"max":max(cold),"mean":sum(cold)/len(cold)},
  "warm_query_seconds":{"min":min(warm),"median":sorted(warm)[len(warm)//2],"max":max(warm),"mean":sum(warm)/len(warm)},
  "candidate_count":{"min":min(counts),"median":sorted(counts)[len(counts)//2],"max":max(counts),"mean":sum(counts)/len(counts)},
  "formula_query_seconds":{"min":min(formula_times),"median":sorted(formula_times)[len(formula_times)//2],"max":max(formula_times),"mean":sum(formula_times)/len(formula_times)},
  "formula_candidate_count":{"min":min(formula_counts),"median":sorted(formula_counts)[len(formula_counts)//2],"max":max(formula_counts),"mean":sum(formula_counts)/len(formula_counts)},
}
Path("results/e01-core-microprofile.json").write_text(json.dumps(summary,indent=2,sort_keys=True))
print(json.dumps(summary,indent=2,sort_keys=True))
shutil.rmtree(root)
