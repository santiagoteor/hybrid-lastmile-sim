#!/usr/bin/env python3
"""Selección territorial reproducible de centros logísticos; no ejecuta OSRM ni ILS."""
import argparse
import io
import json
import re
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

def distances_km(lat, lon, centers):
    """Haversine, en kilómetros, para N clientes x M centros."""
    a=np.radians(np.asarray(lat, dtype=float))[:,None]
    b=np.radians(np.asarray(lon, dtype=float))[:,None]
    c=np.radians(centers["Latitude"].to_numpy(dtype=float))[None,:]
    d=np.radians(centers["Longitude"].to_numpy(dtype=float))[None,:]
    h=np.sin((c-a)/2)**2+np.cos(a)*np.cos(c)*np.sin((d-b)/2)**2
    return 6371.0088*2*np.arcsin(np.sqrt(np.clip(h,0,1)))

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--records",required=True,help="records_classified.csv")
    p.add_argument("--results-zip",required=True,nargs="+",help="ZIP(s) con ejecuciones M6")
    p.add_argument("--output",default="results/warehouse_selection")
    p.add_argument("--count",type=int,default=3)
    args=p.parse_args()
    out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
    records=pd.read_csv(args.records)
    cc=records[pd.to_numeric(records["Type_Group_Code"],errors="coerce")==3].copy()
    cc=cc.dropna(subset=["Latitude","Longitude","Location_Cluster_ID"])
    # Un mismo cluster físico puede aparecer en varios registros.
    cc=cc.sort_values(["Location_Cluster_ID","ID"]).drop_duplicates("Location_Cluster_ID").reset_index(drop=True)
    if not 1<=args.count<=len(cc):raise ValueError("Número de almacenes fuera de rango")
    runs={}
    for file in args.results_zip:
        with zipfile.ZipFile(file) as z:
            names=z.namelist()
            for n in names:
                if not n.endswith("/geography/customer_neighborhoods.csv"):continue
                prefix=n.rsplit("/geography/",1)[0]
                alloc=prefix+"/allocations/customer_assignments.csv"
                if alloc not in names:continue
                geo=pd.read_csv(z.open(n))
                assignments=pd.read_csv(z.open(alloc),usecols=["customer_id","delivery_mode"])
                vans=assignments.loc[assignments.delivery_mode=="VAN",["customer_id"]].drop_duplicates()
                customers=geo.merge(vans,on="customer_id",how="inner",validate="one_to_one")
                if customers.empty:continue
                key=prefix.rsplit("/",1)[-1]
                if key in runs:raise ValueError(f"Ejecución duplicada: {key}")
                runs[key]=customers
    if not runs:raise ValueError("No se encontraron clientes VAN con coordenadas en los ZIP.")
    # Cada semilla pesa igual; dentro de ella los clientes pesan según sus paquetes.
    matrices={k:distances_km(v.latitude,v.longitude,cc).astype(np.float32) for k,v in runs.items()}
    weights={k:v.packages.to_numpy(dtype=float)/v.packages.sum()/len(runs) for k,v in runs.items()}
    selected=[]
    current={k:np.full(len(v),np.inf,dtype=np.float32) for k,v in runs.items()}
    steps=[]
    for step in range(args.count):
        scores=np.zeros(len(cc))
        for k,dist in matrices.items():
            scores+=(np.minimum(current[k][:,None],dist)*weights[k][:,None]).sum(axis=0)
        scores[selected]=np.inf
        chosen=int(np.argmin(scores)) # desempate por ID, tras ordenar
        selected.append(chosen)
        for k,dist in matrices.items():current[k]=np.minimum(current[k],dist[:,chosen])
        steps.append({"n_warehouses":step+1,"warehouse_added":str(cc.iloc[chosen].Location_Cluster_ID),
                      "weighted_mean_distance_km":round(float(sum((current[k]*weights[k]).sum() for k in runs)),5)})
    chosen_centers=cc.iloc[selected][["Location_Cluster_ID","Location","City","Latitude","Longitude"]].copy()
    chosen_centers.insert(0,"selection_order",range(1,len(chosen_centers)+1))
    chosen_centers.to_csv(out/"selected_warehouses.csv",index=False)
    pd.DataFrame(steps).to_csv(out/"selection_steps.csv",index=False)
    summaries=[]; assignments=[]
    for k,v in runs.items():
        dist=matrices[k][:,selected]
        idx=np.argmin(dist,axis=1)
        min_dist=dist[np.arange(len(idx)),idx]
        row=v[["customer_id","packages"]].copy()
        row.insert(0,"run_id",k)
        row["warehouse_id"]=cc.iloc[np.asarray(selected)[idx]].Location_Cluster_ID.to_numpy()
        row["distance_km"]=min_dist
        assignments.append(row)
        for j in selected:
            mask=(row.warehouse_id==cc.iloc[j].Location_Cluster_ID)
            summaries.append({"run_id":k,"warehouse_id":cc.iloc[j].Location_Cluster_ID,
                              "customers":int(mask.sum()),"packages":int(row.loc[mask,"packages"].sum()),
                              "mean_distance_km":round(float(np.average(row.loc[mask,"distance_km"],weights=row.loc[mask,"packages"])) if mask.any() else 0,5)})
    pd.DataFrame(summaries).to_csv(out/"warehouse_load_by_run.csv",index=False)
    pd.concat(assignments,ignore_index=True).to_csv(out/"customer_warehouse_assignments.csv",index=False)
    report={"method":"greedy weighted geographic p-median approximation",
            "optimality":"heuristic; not guaranteed global optimum",
            "distance":"Haversine straight-line km, not OSRM or route km",
            "scope":"VAN customers only; no inventory or routing constraints",
            "seed_weighting":"equal run weight; packages weighted within each run",
            "candidate_records":int(len(records[records.Type_Group_Code==3])),
            "unique_candidate_clusters":len(cc),
            "runs":sorted(runs),"run_count":len(runs),"warehouse_count":args.count,
            "selected_warehouse_ids":chosen_centers.Location_Cluster_ID.tolist(),
            "selection_steps":steps}
    (out/"methodology.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"output":str(out),"selected":report["selected_warehouse_ids"],
                      "runs":len(runs)},ensure_ascii=False,indent=2))
if __name__=="__main__":main()
