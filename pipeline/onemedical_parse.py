#!/usr/bin/env python3
"""Parse a One Medical records export into onemedical.json for the portal.

Inputs (one export folder, e.g. /root/health-data/onemedical/export_2026-10-05/):
  - ehi/fhir_*.json        FHIR bundle from One Medical's EHI export (vitals, immunizations, encounters)
  - records.txt            text of the "Medical Records Request" PDF (pdftotext -layout), for visit notes
Output: /root/health-data/onemedical/onemedical.json with visits, vitals and immunizations.

Usage: onemedical_parse.py [export folder] [output json]
"""
import glob, json, re, sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

SRC = Path(sys.argv[1] if len(sys.argv) > 1 else sorted(glob.glob("/root/health-data/onemedical/export_*"))[-1])
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "/root/health-data/onemedical/onemedical.json")

bundle = json.load(open(sorted(SRC.glob("ehi/fhir_*.json"))[-1]))
res = [e.get("resource", e) for e in bundle.get("entry", [])]


def code_text(c):
    return (c or {}).get("text") or next((x.get("display") for x in (c or {}).get("coding", []) if x.get("display")), "")


# vitals by date
VITAL = {"systolic pressure": "sys", "diastolic pressure": "dia", "heart rate": "pulse", "weight": "weight_lb", "height": "height_in",
         "bmi": "bmi", "temperature": "temp_f", "pulse oximetry": "spo2", "respiratory rate": "rr"}
vitals = defaultdict(dict)
for r in res:
    if r["resourceType"] != "Observation":
        continue
    name = code_text(r.get("code")).lower()
    key = VITAL.get(name)
    q = r.get("valueQuantity") or {}
    if key and q.get("value") is not None and r.get("effectiveDateTime"):
        vitals[r["effectiveDateTime"][:10]][key] = q["value"]

immunizations = sorted(({"d": r.get("occurrenceDateTime", "")[:10], "vaccine": code_text(r.get("vaccineCode"))}
                        for r in res if r["resourceType"] == "Immunization" and r.get("status", "completed") == "completed"),
                       key=lambda x: x["d"])

# visit notes from the records PDF text
L = (SRC / "records.txt").read_text(errors="ignore").splitlines()
visits = []
i = 0
while i < len(L):
    m = re.match(r"^BEGIN - (Office Visit|Walk-in Visit|Outside Laboratory Order)", L[i].strip())
    if not m:
        i += 1
        continue
    j = next((k for k in range(i, min(len(L), i + 400)) if L[k].strip().startswith("END - ")), i + 60)
    blk = L[i:j]
    text = "\n".join(blk)
    d = re.search(r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun) (\w{3}) (\d{1,2}) (\d{4}) @ (\d{1,2}:\d{2} [AP]M)", text)
    date = datetime.strptime(f"{d[2]} {d[3]} {d[4]}", "%b %d %Y").date().isoformat() if d else None
    cc = re.search(r"Chief Complaint\s+(.*)", text)
    cc_pre, cc2 = "", ""
    if cc:  # the PDF layout can wrap the chief complaint onto the lines above and below the label
        k = next(n for n, l in enumerate(blk) if l.strip().startswith("Chief Complaint"))
        prv = blk[k - 1].strip() if k >= 1 else ""
        cc_pre = prv if prv and not prv.startswith(("BEGIN", "Note")) and "California Street" not in prv else ""
        nxt = blk[k + 1].strip() if k + 1 < len(blk) else ""
        cc2 = nxt if nxt and not nxt.startswith(("Note Type", "Note Title")) else ""
    reason = re.sub(r"\s+", " ", " ".join(x for x in (cc_pre, cc[1].strip() if cc else "", cc2) if x)).strip()
    reason = re.sub(r"^(Walk-in Visit|OV):\s*", "", reason).replace("(", "").replace(")", "").strip()
    reason = re.sub(r"\b(\w+)( \1\b)+", r"\1", reason)  # the wrapped line can be picked up twice
    if m[1] == "Outside Laboratory Order":
        reason = "Labs ordered by Sutter endocrinology, drawn at One Medical (LabCorp)"
    who = re.search(r"([A-Z][a-z]+ [A-Z][a-z]+(?:, (?:MD|DO|NP|FNP|PA)(?:, [A-Z]+)?)?) \(NPI", text.replace("\n", " ").replace("  ", " "))
    who = who[1] if who else (re.search(r"Signed By\s+([A-Z][a-z]+ [A-Z][a-z]+)", text) or [None, None])[1]
    assessment = re.findall(r"^(.*?) - ([A-Z]\d{2}(?:\.\d+)?)\s*$", text, re.M)
    issues = [a[0].strip() for a in assessment if a[0].strip() and not a[0].strip().lower().startswith(("venipuncture", "covid-19 test sample", "sars-cov-2 covid-19 vaccine"))]
    procs = []
    for p in re.findall(r"^(SARS-CoV-2 (?:PCR swab|IgG antibody|COVID-19 vaccine)[^\n]*)$", text, re.M):
        procs.append({"SARS-CoV-2 PCR swab": "COVID-19 PCR test", "SARS-CoV-2 IgG antibody": "COVID-19 antibody test"}.get(p.strip(), "COVID-19 vaccine" if "vaccine" in p else p.strip()))
    vtype = {"Office Visit": "Office visit", "Walk-in Visit": "Walk-in", "Outside Laboratory Order": "Lab draw"}[m[1]]
    v = vitals.get(date, {})
    vis = {}
    if v.get("sys") and v.get("dia"):
        vis["bp"] = [int(v["sys"]), int(v["dia"])]
    for k_src, k_dst in (("pulse", "pulse"), ("weight_lb", "weight_lb"), ("bmi", "bmi"), ("temp_f", "temp_f"), ("spo2", "spo2")):
        if v.get(k_src) is not None and vtype == "Office visit":
            vis[k_dst] = v[k_src]
    if vtype != "Office visit":
        vis = {}
    visits.append({"date": date, "time": d[5] if d else None, "type": vtype, "clinician": who, "place": "One Medical",
                   "reason": reason, "issues": issues or ([reason] if reason else []), "vitals": vis,
                   "procedures": procs})
    i = j + 1

visits.sort(key=lambda x: (x["date"] or "", x["time"] or ""))
OUT.write_text(json.dumps({"source": str(SRC), "visits": visits, "immunizations": immunizations,
                           "vitals_by_date": vitals}, indent=1))
print(f"wrote {OUT}: {len(visits)} visits, {len(immunizations)} immunizations, vitals on {sorted(vitals)}")
