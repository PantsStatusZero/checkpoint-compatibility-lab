from __future__ import annotations

import json
import requests

SLUG="tyreejones393/casmi26-raw-index"
urls=[
    f"https://www.kaggle.com/api/v1/datasets/view/{SLUG}",
    f"https://www.kaggle.com/api/v1/datasets/list?search=casmi26-raw-index",
]
out=[]
for url in urls:
    r=requests.get(url,timeout=60)
    row={"url":url,"status":r.status_code,"content_type":r.headers.get("content-type"),"bytes":len(r.content)}
    try:
        data=r.json()
        if isinstance(data,dict):
            row["keys"]=sorted(data.keys())
            if "datasetFiles" in data:
                row["datasetFiles"]=[
                    {k:f.get(k) for k in ("name","totalBytes","creationDate","ref","url")}
                    for f in data["datasetFiles"]
                ]
            if "files" in data:
                row["files"]=[
                    {k:item.get(k) for k in ("name","totalBytes","creationDate","ref","url")}
                    for item in data["files"]
                ]
            row["title"]=data.get("title")
            row["ref"]=data.get("ref")
            row["licenseName"]=data.get("licenseName")
            row["totalBytes"]=data.get("totalBytes")
        elif isinstance(data,list):
            row["list_count"]=len(data)
            row["items"]=[{"ref":x.get("ref"),"title":x.get("title"),"totalBytes":x.get("totalBytes")} for x in data[:10]]
    except Exception as e:
        row["json_error"]=repr(e)
        row["prefix"]=r.text[:500]
    out.append(row)
print(json.dumps(out,indent=2,sort_keys=True))
