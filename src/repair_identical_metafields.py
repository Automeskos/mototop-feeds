"""One-time, allowlisted Shopify variant metafield correction; dry run by default."""
from __future__ import annotations
import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path

import requests

API_VERSION = "2026-10"
APPROVED = Path("config/approved-identical-variants.csv")
FEED = Path("feeds/warehouse-status.csv")
REPORT = Path("reports-identical")
STATUS_COL = "Variant Metafield: custom.tiekejo_sandelis [single_line_text_field]"
NODES = """query($ids:[ID!]!){nodes(ids:$ids){... on ProductVariant{id sku title product{id title} metafield(namespace:"custom",key:"tiekejo_sandelis"){value}}}}"""
SET = """mutation($fields:[MetafieldsSetInput!]!){metafieldsSet(metafields:$fields){userErrors{field message}}}"""


def csv_rows(file):
    with file.open(encoding="utf-8-sig",newline="") as stream:
        return list(csv.DictReader(stream))


def save(name, header, data):
    with (REPORT / name).open("w",encoding="utf-8-sig",newline="") as f:
        writer=csv.writer(f)
        writer.writerow(header)
        writer.writerows(data)


def clean(value):
    return " ".join(str(value or "").casefold().split())


class Client:
    def __init__(self):
        self.shop = os.environ["SHOPIFY_SHOP"].strip().removeprefix("https://").rstrip("/")
        if not self.shop.endswith(".myshopify.com"):
            raise ValueError("Invalid SHOPIFY_SHOP (must end in .myshopify.com)")
        token_resp = requests.post(f"https://{self.shop}/admin/oauth/access_token",data={
            "grant_type":"client_credentials",
            "client_id":os.environ["SHOPIFY_CLIENT_ID"],
            "client_secret":os.environ["SHOPIFY_CLIENT_SECRET"],
        },timeout=30)
        token_resp.raise_for_status()
        token=token_resp.json()["access_token"]
        self.session=requests.Session()
        self.session.headers.update({"X-Shopify-Access-Token":token,"Content-Type":"application/json"})
        self.endpoint=f"https://{self.shop}/admin/api/{API_VERSION}/graphql.json"

    def gql(self,query,variables):
        for attempt in range(8):
            response=self.session.post(self.endpoint,json={"query":query,"variables":variables},timeout=90)
            if response.status_code==429 or response.status_code>=500:
                time.sleep(min(30,2**attempt));continue
            response.raise_for_status()
            payload=response.json()
            errors=payload.get("errors",[])
            if errors:
                if any(x.get("extensions",{}).get("code")=="THROTTLED" for x in errors):
                    time.sleep(min(30,2**attempt));continue
                raise RuntimeError(json.dumps(errors,ensure_ascii=False))
            return payload["data"]
        raise RuntimeError("Shopify API retries exhausted")


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--apply",action="store_true")
    parser.add_argument("--limit",type=int,default=0)
    args=parser.parse_args()
    if args.limit<0: raise ValueError("Negative limit")
    REPORT.mkdir(exist_ok=True)
    approved=csv_rows(APPROVED)
    feed=csv_rows(FEED)
    feed_by_sku={}
    for item in feed:
        sku=item.get("Variant SKU", "").strip().lstrip("'")
        status=item.get(STATUS_COL, "").strip()
        feed_by_sku.setdefault(sku,set()).add(status)
    if not approved:
        raise ValueError("Allowlist empty")
    seen=set()
    for item in approved:
        vid=item["variant_id"]
        if vid in seen: raise ValueError(f"Duplicate variant in allowlist: {vid}")
        seen.add(vid)
    api=Client()
    nodes={}
    for start in range(0,len(approved),50):
        ids=["gid://shopify/ProductVariant/"+r["variant_id"] for r in approved[start:start+50]]
        result=api.gql(NODES,{"ids":ids})["nodes"]
        for entry in result:
            if entry:
                nodes[entry["id"]]=entry
    ready=[];issues=[];unchanged=[]
    for item in approved:
        sku=item["sku"].strip()
        vid="gid://shopify/ProductVariant/"+item["variant_id"]
        pid="gid://shopify/Product/"+item["product_id"]
        current=nodes.get(vid)
        if not current or not current.get("product"):
            issues.append([sku,item["variant_id"],"Variant not found"]);continue
        if current["sku"] != sku or current["product"]["id"] != pid:
            issues.append([sku,item["variant_id"],"SKU or product ID mismatch"]);continue
        if clean(current["product"]["title"]) != clean(item["product_title"]) or clean(current["title"]) != clean(item["variant_title"]):
            issues.append([sku,item["variant_id"],"Title or variant size mismatch"]);continue
        expected=item["expected_status"]
        if expected!="2-4d" or feed_by_sku.get(sku)!= {expected}:
            issues.append([sku,item["variant_id"],f"Feed status not confirmed: {sorted(feed_by_sku.get(sku,set()))}"]);continue
        prior=(current.get("metafield") or {}).get("value","")
        if prior==expected:
            unchanged.append([sku,item["variant_id"],prior]);continue
        if prior not in ("",None):
            issues.append([sku,item["variant_id"],f"Existing metafield is different: {prior}"]);continue
        ready.append([sku,vid,expected])
    if args.limit:
        ready=ready[:args.limit]
    save("plan.csv",["sku","variant_id","new_status"],ready)
    save("skipped.csv",["sku","variant_id","reason"],issues)
    save("unchanged.csv",["sku","variant_id","existing_status"],unchanged)
    updated=[];failed=[]
    if args.apply:
        for start in range(0,len(ready),25):
            batch=ready[start:start+25]
            fields=[{"ownerId":vid,"namespace":"custom","key":"tiekejo_sandelis","type":"single_line_text_field","value":status} for _,vid,status in batch]
            try:
                result=api.gql(SET,{"fields":fields})["metafieldsSet"]
                if result["userErrors"]:raise RuntimeError(str(result["userErrors"]))
                updated.extend([[sku,vid,status] for sku,vid,status in batch])
            except Exception as error:
                failed.extend([[sku,vid,str(error)] for sku,vid,_ in batch])
    save("updated.csv",["sku","variant_id","status"],updated)
    save("failed.csv",["sku","variant_id","error"],failed)
    print(json.dumps({"apply":args.apply,"planned":len(ready),"unchanged":len(unchanged),"skipped":len(issues),"updated":len(updated),"failed":len(failed)},ensure_ascii=False))
    return 1 if failed else 0


if __name__=="__main__":
    sys.exit(main())
