from __future__ import annotations
import argparse
import csv
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
import requests

API_VERSION = "2026-10"
PRODUCT_VARIANTS = """query($cursor:String){productVariants(first:100,after:$cursor){nodes{id sku product{id handle}} pageInfo{hasNextPage endCursor}}}"""
UPDATE = """mutation($product:ProductUpdateInput!){productUpdate(product:$product){product{id handle} userErrors{field message}}}"""
SET_FIELDS = """mutation($metafields:[MetafieldsSetInput!]!){metafieldsSet(metafields:$metafields){userErrors{field message}}}"""


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


class Shopify:
    def __init__(self):
        self.shop = os.environ["SHOPIFY_SHOP"].strip().removeprefix("https://").rstrip("/")
        if not self.shop.endswith(".myshopify.com"):
            raise ValueError("SHOPIFY_SHOP turi būti parduotuve.myshopify.com")
        client_id = os.environ["SHOPIFY_CLIENT_ID"]
        secret = os.environ["SHOPIFY_CLIENT_SECRET"]
        response = requests.post(f"https://{self.shop}/admin/oauth/access_token", data={
            "grant_type":"client_credentials", "client_id":client_id,"client_secret":secret
        }, timeout=30)
        response.raise_for_status()
        self.token = response.json()["access_token"]
        self.url = f"https://{self.shop}/admin/api/{API_VERSION}/graphql.json"
        self.session = requests.Session()
        self.session.headers.update({"X-Shopify-Access-Token":self.token,"Content-Type":"application/json"})

    def gql(self, query, variables):
        for attempt in range(8):
            resp = self.session.post(self.url,json={"query":query,"variables":variables},timeout=90)
            if resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(min(30,2**attempt));continue
            resp.raise_for_status()
            payload=resp.json()
            errors=payload.get("errors") or []
            if errors:
                if any(e.get("extensions",{}).get("code")=="THROTTLED" for e in errors):
                    time.sleep(min(30,2**attempt));continue
                raise RuntimeError(str(errors))
            data=payload["data"]
            return data
        raise RuntimeError("Shopify API nepavyko po pakartojimų")

    def catalog(self):
        by_sku=defaultdict(list)
        cursor=None
        pages=0
        while True:
            result=self.gql(PRODUCT_VARIANTS,{"cursor":cursor})["productVariants"]
            for v in result["nodes"]:
                if v.get("sku"):
                    by_sku[v["sku"].strip()].append(v)
            pages += 1
            if not result["pageInfo"]["hasNextPage"]:break
            nxt=result["pageInfo"]["endCursor"]
            if nxt==cursor:raise RuntimeError("Nepavyko pereiti prie kito katalogo puslapio")
            cursor=nxt
            if pages>5000:raise RuntimeError("Per daug katalogo puslapių")
        print(f"Shopify kataloge skirtingų SKU: {len(by_sku)}")
        return by_sku


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--mode",choices=["urls","statuses"],required=True)
    p.add_argument("--apply",action="store_true",help="Be šio parametro nieko nekeičia")
    p.add_argument("--limit",type=int,default=0,help="0=visi; testui pvz. 10")
    args=p.parse_args()
    source=Path("url-changes.csv" if args.mode=="urls" else "feeds/warehouse-status.csv")
    if not source.exists():raise FileNotFoundError(source)
    records=read_csv(source)
    if args.mode=="urls":
        expected={"sku","handle"}
    else:
        expected={"Variant SKU","Variant Metafield: custom.tiekejo_sandelis [single_line_text_field]"}
    if not records or not expected.issubset(records[0]):
        raise ValueError("Neteisingi failo stulpeliai")
    api=Shopify()
    by_sku=api.catalog()
    plans=[];issues=[];seen=set();destination_handles={}
    allowed={"2-3d","2-5d","5-15d","isparduota"}
    for record in records:
        sku=(record["sku"] if args.mode=="urls" else record["Variant SKU"]).strip().lstrip("'")
        if not sku or sku in seen:
            issues.append([sku,"Dublikatas arba tuščias SKU"]);continue
        seen.add(sku)
        matches=by_sku.get(sku,[])
        if len(matches)!=1:
            issues.append([sku,f"Rasta variantų: {len(matches)}"]);continue
        v=matches[0]
        if args.mode=="urls":
            new=record["handle"].strip()
            if not new or "/" in new or " " in new:
                issues.append([sku,"Netinkamas naujas handle"]);continue
            old=v["product"]["handle"]
            pid=v["product"]["id"]
            if old==new:continue
            if not old.startswith("mototop-"):
                issues.append([sku,f"Dabartinis handle nėra mototop-: {old}"]);continue
            if pid in destination_handles and destination_handles[pid]!=new:
                issues.append([sku,"Tam pačiam produktui siūlomi skirtingi URL"]);continue
            destination_handles[pid]=new
            plans.append((sku,pid,old,new))
        else:
            status=record["Variant Metafield: custom.tiekejo_sandelis [single_line_text_field]"].strip()
            if status not in allowed:
                issues.append([sku,f"Nežinoma būsena: {status}"]);continue
            plans.append((sku,v["id"],"",status))
    # Net jei CSV eilutės atitinka skirtingus SKU, vieno produkto URL keičiame vieną kartą.
    if args.mode=="urls":
        unique=[];pids=set()
        for item in plans:
            if item[1] not in pids:unique.append(item);pids.add(item[1])
        plans=unique
    # Nevykdyti, jei tas pats naujas handle priskirtas keliems skirtingiems produktams.
    if args.mode=="urls":
        handle_pids=defaultdict(set)
        for _,pid,_,new in plans:handle_pids[new].add(pid)
        colliding={h for h,ids in handle_pids.items() if len(ids)>1}
        if colliding:
            plans=[x for x in plans if x[3] not in colliding]
            issues.extend([["",f"Dubliuojamas handle: {h}"] for h in sorted(colliding)])
    Path("reports").mkdir(exist_ok=True)
    with open("reports/problems.csv","w",encoding="utf-8",newline="") as f:
        w=csv.writer(f);w.writerow(["sku","problem"]);w.writerows(issues)
    with open("reports/plan.csv","w",encoding="utf-8",newline="") as f:
        w=csv.writer(f);w.writerow(["sku","shopify_id","old_handle","target"]);w.writerows(plans)
    print(f"Režimas: {args.mode}; apply={args.apply}; planas={len(plans)}; atmesta={len(issues)}")
    if not args.apply:
        print("DRY RUN: Shopify pakeitimų nėra. Žr. reports/plan.csv ir reports/problems.csv")
        return 0
    work=plans[:args.limit] if args.limit>0 else plans
    done=[];failed=[]
    if args.mode=="urls":
        for sku,pid,old,new in work:
            try:
                result=api.gql(UPDATE,{"product":{"id":pid,"handle":new,"redirectNewHandle":True}})["productUpdate"]
                if result["userErrors"]:raise RuntimeError(str(result["userErrors"]))
                if not result.get("product") or result["product"]["handle"]!=new:
                    raise RuntimeError("Nesutampa atnaujintas handle")
                done.append([sku,old,new]);time.sleep(0.15)
            except Exception as e:
                failed.append([sku,str(e)])
    else:
        # Batches limited to 25 metafields per call; if batch fails, report without silently ignoring.
        for i in range(0,len(work),25):
            batch=work[i:i+25]
            fields=[{"ownerId":vid,"namespace":"custom","key":"tiekejo_sandelis","type":"single_line_text_field","value":status} for _,vid,_,status in batch]
            try:
                result=api.gql(SET_FIELDS,{"metafields":fields})["metafieldsSet"]
                if result["userErrors"]:raise RuntimeError(str(result["userErrors"]))
                done.extend([[sku,status] for sku,_,_,status in batch]);time.sleep(0.15)
            except Exception as e:
                failed.extend([[sku,str(e)] for sku,_,_,_ in batch])
    with open("reports/updated.csv","w",encoding="utf-8",newline="") as f:
        w=csv.writer(f);w.writerows(done)
    with open("reports/failed.csv","w",encoding="utf-8",newline="") as f:
        w=csv.writer(f);w.writerows(failed)
    print(f"Atnaujinta: {len(done)}, klaidų: {len(failed)}. Žr. reports/")
    return 1 if failed else 0

if __name__=="__main__":sys.exit(main())
