"""Match the SD-WAN Cisco/Juniper network inventory to ASSET_MASTER on ci_no, serial_no, ip_address and hostname.

  python tools/sdwan_match.py preview --sdwan "S D Wan Cisco Detail.xlsx" --master IT_Asset_Master_2026-Q2.xlsx
      -> writes SDWAN_Match_Preview.xlsx (READ-ONLY analysis; master and database are not touched)

  python tools/sdwan_match.py apply --preview SDWAN_Match_Preview.xlsx --master IT_Asset_Master_2026-Q2.xlsx
      -> applies only the rows marked APPROVE=YES in PROPOSED_UPDATES to the master workbook and the database
"""
import argparse
import collections
import re
import sys
from pathlib import Path

import pandas as pd

SHEET = "Network Inventry"
FIELD_TO_MASTER = {"CI_NO": "CI_NO", "SERIAL_NO": "SERIAL_NO", "IP_ADDRESS": "IP_ADDRESS", "HOSTNAME": "HOSTNAME"}


def s(v):
    if v is None or (not isinstance(v, str) and pd.isna(v)):
        return None
    t = re.sub(r"\s+", " ", str(v).replace("\xa0", " ")).strip()
    return t or None


def nk(v):
    """Normalised comparison key: upper-case, spaces removed."""
    t = s(v)
    return re.sub(r"\s+", "", t).upper() if t else None


def loose(v):
    t = s(v)
    return re.sub(r"[^A-Z0-9]", "", t.upper()) if t else None


def ip_only(v):
    t = s(v)
    return t.split("/")[0].strip() if t else None


def load_sdwan(path):
    d = pd.read_excel(path, sheet_name=SHEET, dtype=object)
    rows = []
    for i, r in d.iterrows():
        rows.append({"SDWAN_ROW": i + 2, "SDWAN_SR": s(r["Sr,No"]), "MAKE": (s(r["Make"]) or "").upper(), "MODEL": s(r["Model"]),
                     "TYPE": s(r["Type"]), "LOCATION": s(r["Locatation"]), "HOSTNAME": s(r["Host Name"]), "CI_NO": s(r["ASSAT ID"]),
                     "SERIAL_NO": s(r["Sr.NO"]), "IP_ADDRESS": ip_only(r["SWITECH IP"]), "IP_RAW": s(r["SWITECH IP"]),
                     "VERSION": s(r["Version"])})
    return pd.DataFrame(rows)


def load_master(path):
    m = pd.read_excel(path, sheet_name="ASSET_MASTER", dtype=object)
    m = m[m["ASSET_KEY"].notna()].copy()
    return m


def build_indexes(m):
    idx = {"CI_NO": collections.defaultdict(set), "SERIAL_NO": collections.defaultdict(set),
           "IP_ADDRESS": collections.defaultdict(set), "HOSTNAME": collections.defaultdict(set)}
    for k, ci, sn, ip, h in zip(m["ASSET_KEY"], m["CI_NO"], m["SERIAL_NO"], m["IP_ADDRESS"], m["HOSTNAME"]):
        for f, v, fn in (("CI_NO", ci, nk), ("SERIAL_NO", sn, nk), ("IP_ADDRESS", ip_only(ip), nk), ("HOSTNAME", h, loose)):
            key = fn(v)
            if key:
                idx[f][key].add(k)
    return idx


def match(sd, m):
    idx = build_indexes(m)
    mi = m.set_index("ASSET_KEY")
    fn = {"CI_NO": nk, "SERIAL_NO": nk, "IP_ADDRESS": nk, "HOSTNAME": loose}
    out = []
    for _, r in sd.iterrows():
        hits = {}
        for f in ("CI_NO", "SERIAL_NO", "IP_ADDRESS", "HOSTNAME"):
            key = fn[f](r[f])
            hits[f] = set(idx[f].get(key, set())) if key else set()
        allk = set().union(*hits.values())
        rec = dict(r)
        rec["KEYS_MATCHED"] = ",".join(f for f in hits if hits[f])
        if not allk:
            rec.update(STATUS="NOT_IN_MASTER", MASTER_KEY=None)
        elif len(allk) == 1:
            rec.update(STATUS="MATCHED", MASTER_KEY=next(iter(allk)))
        else:
            # several master assets touched: pick the one hit by most keys; conflict if tie or if others are hit by identifying keys
            score = collections.Counter(k for f in hits for k in hits[f])
            attr = set().union(hits["SERIAL_NO"], hits["IP_ADDRESS"], hits["HOSTNAME"])
            ci_t = hits["CI_NO"]
            best, n = score.most_common(1)[0]
            tie = [k for k, c in score.items() if c == n]
            attr_best = None
            if len(attr) == 1:
                attr_best = next(iter(attr))
            elif attr:
                cnt = collections.Counter(k for f in ("SERIAL_NO", "IP_ADDRESS", "HOSTNAME") for k in hits[f])
                top = cnt.most_common(2)
                attr_best = top[0][0] if len(top) == 1 or top[0][1] > top[1][1] else None
            rec.update(STATUS="CONFLICT", MASTER_KEY=attr_best if attr_best else (best if len(tie) == 1 else None),
                       CONFLICT_DETAIL="; ".join(f"{f}->{','.join(sorted(v))}" for f, v in hits.items() if v),
                       ATTR_KEYS=",".join(sorted(attr)), CI_HIT=",".join(sorted(ci_t)))
        out.append(rec)
    res = pd.DataFrame(out).astype(object)
    res = res.where(res.notna(), None)
    for c in ("ATTR_KEYS", "CI_HIT"):
        if c not in res.columns:
            res[c] = None
    res["CONFLICT_TYPE"] = None
    res["PAIR_WITH_ROW"] = None
    # per-field comparison for matched / conflict-with-best
    fields = ["CI_NO", "SERIAL_NO", "IP_ADDRESS", "HOSTNAME"]
    for f in fields:
        res[f"{f}_MASTER"] = None
        res[f"{f}_CMP"] = None
    for i, r in res.iterrows():
        k = r["MASTER_KEY"]
        if not k or k not in mi.index:
            continue
        for f in fields:
            mv = s(mi.at[k, f]) if f != "IP_ADDRESS" else ip_only(mi.at[k, f])
            sv = r[f]
            res.at[i, f"{f}_MASTER"] = mv
            if sv is None and mv is None:
                c = "BOTH_BLANK"
            elif sv is None:
                c = "SDWAN_BLANK"
            elif mv is None:
                c = "MASTER_BLANK"
            elif fn[f](sv) == fn[f](mv):
                c = "SAME"
            else:
                c = "DIFFERENT"
            res.at[i, f"{f}_CMP"] = c
        res.at[i, "MASTER_CLASS"] = mi.at[k, "ASSET_CLASS"]
        res.at[i, "MASTER_TYPE"] = mi.at[k, "ASSET_TYPE"]
        res.at[i, "MASTER_MODEL"] = s(mi.at[k, "MODEL"])
        res.at[i, "MASTER_MAKE"] = s(mi.at[k, "MAKE"])
    conf = res[res["STATUS"] == "CONFLICT"]
    by_ci = {r["CI_NO"]: i for i, r in conf.iterrows() if r["CI_NO"]}
    for i, r in conf.iterrows():
        ci, tgt = r["CI_NO"], r["MASTER_KEY"]
        if ci and tgt and ci != tgt:
            j = by_ci.get(tgt)
            if j is not None and conf.at[j, "MASTER_KEY"] == ci:
                res.at[i, "CONFLICT_TYPE"], res.at[i, "PAIR_WITH_ROW"] = "CI_SWAP_PAIR", conf.at[j, "SDWAN_ROW"]
            else:
                res.at[i, "CONFLICT_TYPE"] = "CI_MISMATCH"
        else:
            res.at[i, "CONFLICT_TYPE"] = "MULTI_KEY_CONFLICT"
    return res


def note_for(a, b):
    if a is None or b is None:
        return ""
    la, lb = loose(a), loose(b)
    if la and lb and la != lb and (lb.startswith(la) or la.startswith(lb)):
        return "one value is a truncated form of the other"
    return ""


def proposals(res):
    rows = []
    for _, r in res.iterrows():
        keys = [x for x in (r["KEYS_MATCHED"] or "").split(",") if x]
        matched_like = r["STATUS"] == "MATCHED" or (r["STATUS"] == "CONFLICT" and r["CONFLICT_TYPE"] == "MULTI_KEY_CONFLICT" and r["MASTER_KEY"])
        if matched_like:
            strong = r["STATUS"] == "MATCHED"
            for f in ("CI_NO", "SERIAL_NO", "IP_ADDRESS", "HOSTNAME"):
                c = r[f"{f}_CMP"]
                others = [x for x in keys if x != f]
                conf = "HIGH" if (strong and len(others) >= 2) else "MEDIUM"
                if c == "MASTER_BLANK":
                    rows.append((r["MASTER_KEY"], f, None, r[f], "FILL BLANK", conf, "USE SD-WAN", r["KEYS_MATCHED"], "", r["SDWAN_ROW"]))
                elif c == "DIFFERENT":
                    note = note_for(r[f"{f}_MASTER"], r[f])
                    if not strong:
                        note = (note + "; " if note else "") + "this value is currently recorded on another master asset (see CONFLICTS sheet)"
                    if f == "CI_NO":
                        rows.append((r["MASTER_KEY"], f, r[f"{f}_MASTER"], r[f], "RELABEL CI (changes ASSET_KEY)", conf, "REVIEW", r["KEYS_MATCHED"], note, r["SDWAN_ROW"]))
                    else:
                        rec = "USE SD-WAN" if (strong and len(others) >= 2) else "REVIEW"
                        rows.append((r["MASTER_KEY"], f, r[f"{f}_MASTER"], r[f], "CHANGE VALUE", conf, rec, r["KEYS_MATCHED"], note, r["SDWAN_ROW"]))
        elif r["STATUS"] == "CONFLICT" and r["CONFLICT_TYPE"] in ("CI_SWAP_PAIR", "CI_MISMATCH") and r["MASTER_KEY"]:
            master_ci = r["CI_NO_MASTER"]
            note = f"serial/IP/hostname point to master asset {r['MASTER_KEY']} (its CI there: {master_ci}) but SD-WAN says CI is {r['CI_NO']}"
            if r["CONFLICT_TYPE"] == "CI_SWAP_PAIR":
                note += f"; swapped with SD-WAN row {r['PAIR_WITH_ROW']}"
            rows.append((r["MASTER_KEY"], "CI_NO", master_ci, r["CI_NO"], "RELABEL CI (changes ASSET_KEY)", "MEDIUM", "REVIEW", r["KEYS_MATCHED"], note, r["SDWAN_ROW"]))
    return pd.DataFrame(rows, columns=["ASSET_KEY", "FIELD", "CURRENT_MASTER", "PROPOSED_SDWAN", "ACTION", "CONFIDENCE", "RECOMMENDATION", "MATCHED_ON", "NOTE", "SDWAN_ROW"])


def preview(sdwan, master, out):
    sd = load_sdwan(sdwan)
    m = load_master(master)
    res = match(sd, m)
    prop = proposals(res)
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    rel = prop[prop["ACTION"].str.startswith("RELABEL")]
    for _, r in rel.iterrows():
        parent[find(r["ASSET_KEY"])] = find(r["PROPOSED_SDWAN"])
    prop["GROUP"] = None
    roots = {}
    for i, r in rel.iterrows():
        root = find(r["ASSET_KEY"])
        roots.setdefault(root, f"G{len(roots) + 1}")
        prop.at[i, "GROUP"] = roots[root]
    sizes = prop["GROUP"].value_counts()
    prop["GROUP_SIZE"] = prop["GROUP"].map(sizes)
    master_keys = set(m["ASSET_KEY"])
    relabeled = set(rel["ASSET_KEY"])
    prop["CHECK"] = "OK"
    for i, r in rel.iterrows():
        tgt = r["PROPOSED_SDWAN"]
        if tgt in master_keys and tgt not in relabeled:
            prop.at[i, "CHECK"] = f"COLLISION: {tgt} already exists in master and is not being relabelled"
    bad_groups = set(prop.loc[prop["CHECK"] != "OK", "GROUP"].dropna())
    prop.loc[prop["GROUP"].isin(bad_groups) & (prop["CHECK"] == "OK"), "CHECK"] = "GROUP HAS A COLLISION - resolve whole group together"
    prop.insert(0, "APPROVE", None)
    net = m[(m["ASSET_CLASS"].isin(["SWITCH", "ROUTER"])) & (m["RECORD_LEVEL"] == "ASSET")]
    used = set(res["MASTER_KEY"].dropna())
    not_in_sd = net[~net["ASSET_KEY"].isin(used)][["ASSET_KEY", "ASSET_CLASS", "ASSET_TYPE", "MAKE", "MODEL", "HOSTNAME", "SERIAL_NO", "IP_ADDRESS", "LOCATION_CODE", "ASSET_STATUS", "REMARKS"]]
    dups = []
    for f in ("CI_NO", "SERIAL_NO", "IP_ADDRESS", "HOSTNAME"):
        col = res[f].map(lambda v: (loose if f == "HOSTNAME" else nk)(v))
        for v, g in res[col.notna() & col.duplicated(keep=False)].groupby(col):
            dups.append((f, v, ", ".join(f"row {x}" for x in g["SDWAN_ROW"]), ", ".join(str(x) for x in g["HOSTNAME"])))
    dups = pd.DataFrame(dups, columns=["FIELD", "VALUE", "SDWAN_ROWS", "HOSTNAMES"])
    st = res["STATUS"].value_counts().to_dict()
    ct = res[res["STATUS"] == "CONFLICT"]["CONFLICT_TYPE"].value_counts().to_dict()
    summ = [("SD-WAN devices (Network Inventry sheet)", len(res)),
            ("  MATCHED to one master asset", st.get("MATCHED", 0)),
            ("  CONFLICT (keys point to different master assets)", st.get("CONFLICT", 0)),
            ("      conflict type: " + ", ".join(f"{k}={v}" for k, v in ct.items()), ""),
            ("  NOT IN MASTER", st.get("NOT_IN_MASTER", 0)),
            ("Master SWITCH / ROUTER assets", len(net)),
            ("  not found in SD-WAN file", len(not_in_sd)),
            ("Proposed FILL BLANK (master empty, SD-WAN has value)", int((prop["ACTION"] == "FILL BLANK").sum())),
            ("Proposed CHANGE VALUE (both filled, differ)", int((prop["ACTION"] == "CHANGE VALUE").sum())),
            ("Proposed RELABEL CI from conflicts (changes ASSET_KEY)", int(prop["ACTION"].str.startswith("RELABEL").sum())),
            ("Duplicate values inside SD-WAN file", len(dups))]
    for f in ("CI_NO", "SERIAL_NO", "IP_ADDRESS", "HOSTNAME"):
        vc = res[res["STATUS"] == "MATCHED"][f"{f}_CMP"].value_counts().to_dict()
        summ.append((f"{f}: " + ", ".join(f"{k}={v}" for k, v in sorted(vc.items())), ""))
    with pd.ExcelWriter(out) as w:
        pd.DataFrame(summ, columns=["CHECK", "COUNT"]).to_excel(w, sheet_name="SUMMARY", index=False)
        prop.to_excel(w, sheet_name="PROPOSED_UPDATES", index=False)
        res[res["STATUS"] == "CONFLICT"].to_excel(w, sheet_name="CONFLICTS", index=False)
        res[res["STATUS"] == "NOT_IN_MASTER"][["SDWAN_ROW", "SDWAN_SR", "MAKE", "MODEL", "TYPE", "LOCATION", "HOSTNAME", "CI_NO", "SERIAL_NO", "IP_RAW"]].to_excel(w, sheet_name="NOT_IN_MASTER", index=False)
        not_in_sd.to_excel(w, sheet_name="MASTER_NOT_IN_SDWAN", index=False)
        dups.to_excel(w, sheet_name="SDWAN_DUPLICATES", index=False)
        res.to_excel(w, sheet_name="MATCH_DETAIL", index=False)
        for ws in w.book.worksheets:
            for col in ws.columns:
                ws.column_dimensions[col[0].column_letter].width = min(40, max(12, max(len(str(c.value or "")) for c in col[:60]) + 2))
            ws.freeze_panes = "A2"
    return res, prop, not_in_sd, dups, summ


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["preview", "apply"])
    ap.add_argument("--sdwan")
    ap.add_argument("--master", required=True)
    ap.add_argument("--preview", default="samples/SDWAN_Match_Preview.xlsx")
    a = ap.parse_args()
    if a.cmd == "preview":
        res, prop, nis, dups, summ = preview(a.sdwan, a.master, a.preview)
        for k, v in summ:
            print(f"{k:70} {v}")
    else:
        sys.exit("apply is not enabled until the preview has been approved")
