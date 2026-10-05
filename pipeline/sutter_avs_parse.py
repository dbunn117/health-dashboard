#!/usr/bin/env python3
"""Parse Sutter My Health Online After Visit Summary (AVS) HTML files into visits.json.

Input: a folder or zip of Sutter_AVS_*.html files (the 2019-2026 export lives in
/root/health-data/sutter/avs/). Output: /root/health-data/sutter/avs/visits.json, one record per
summary with date, time, type, clinician, location, issues addressed, vitals, instructions,
medication changes, labs/imaging ordered, procedures and follow-up. Read-only on the inputs.

Usage: sutter_avs_parse.py [input folder or zip] [output json]
"""
import html, json, re, sys, zipfile
from pathlib import Path

SRC = Path(sys.argv[1] if len(sys.argv) > 1 else "/root/health-data/sutter/avs")
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "/root/health-data/sutter/avs/visits.json")
SECTION_STARTS = ("Icon ", "Today's Visit", "What's Next", "Release of Results", "Your Medication List", "Lab Follow-up",
                  "Help us to better", "Your personalized instructions", "Accurate as of", "Review your updated")


BOILERPLATE = re.compile(r"^(Dear |Thank you for coming|Below is a summary|Take good care|Best - |I ordered the below|No new orders|Please note (in regards|that because)|Your personalized instructions)", re.I)


def text_of(raw):
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    t = re.sub(r"(?s)<!--.*?-->", " ", t)
    t = re.sub(r"(?s)<br\s*/?>|</(p|div|tr|li|h\d|table|td)>", "\n", t)
    t = html.unescape(re.sub(r"<[^>]+>", " ", t))
    return [re.sub(r"[ \t ​]+", " ", l).strip() for l in t.splitlines() if l.strip()]


def block(lines, start_pred, stop_pred=lambda l: l.startswith(SECTION_STARTS)):
    out, on = [], False
    for l in lines:
        if not on and start_pred(l):
            on = True
            continue
        if on:
            if stop_pred(l):
                break
            out.append(l)
    return out


def lbs(s):
    m = re.match(r"(\d+) lb(?: ([\d.]+) oz)?", s)
    return round(int(m[1]) + (float(m[2]) / 16 if m[2] else 0), 1) if m else None


def parse(name, raw):
    L = text_of(raw)
    title = (re.findall(r"<title>(.*?)</title>", raw, re.S) or [""])[0].strip()
    vtype = title.split(" - ")[0].strip()
    head = next((l for l in L if l.startswith("with ")), "")
    m = re.match(r"with (.*?) at (.*)", head)
    clinician, place = (m[1].strip(), m[2].strip()) if m else (None, None)
    if clinician and (re.search(r"\b(XR|CT|MRI|US)\b|PASCDIR|Lab Provider", clinician) or not re.search(r",\s*(MD|DO|NP|PA|RN|PA-C)\b", clinician)):
        clinician = None  # imaging rooms and lab draw stations, not people
    dl = next((l for l in L if l.startswith("Icon Date")), "")
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})(?: (\d{1,2}:\d{2} [AP]M))?", dl)
    date = f"{m[3]}-{int(m[1]):02d}-{int(m[2]):02d}" if m else name.split("_")[2]
    time = m[4] if m and m[4] else None
    if not place:
        m2 = re.search(r"Icon Location (.*?)(?: \d{3}-\d{3}-\d{4})?$", dl)
        place = m2[1].strip() if m2 else None
    issues = []
    for l in L:
        m = re.search(r"The following issues? (?:was|were) addressed: (.*)", l)
        if m:
            issues = [x.strip().rstrip(".") for x in re.split(r";|, and | and (?=[A-Z])", m[1]) if x.strip()]
            break
    vit = {}
    for i, l in enumerate(L):
        for key, label in (("bp", "Blood Pressure "), ("pulse", "Pulse "), ("weight", "Weight "), ("height", "Height "), ("bmi", "BMI "),
                           ("temp", "Temperature"), ("spo2", "Oxygen Saturation ")):
            if l.startswith(label) and not l.startswith("Icon"):
                v = l[len(label):].strip() if key != "temp" else re.sub(r"^\(.*?\)\s*", "", l[len("Temperature"):].strip())
                vit.setdefault(key, v)
    vitals = {}
    if "bp" in vit and re.match(r"\d+/\d+", vit["bp"]):
        s, d = vit["bp"].split("/")[:2]; vitals["bp"] = [int(s), int(re.match(r"\d+", d)[0])]
    if "pulse" in vit and re.match(r"\d+", vit["pulse"]):
        vitals["pulse"] = int(re.match(r"\d+", vit["pulse"])[0])
    if "weight" in vit and lbs(vit["weight"]):
        vitals["weight_lb"] = lbs(vit["weight"])
    if "bmi" in vit:
        try: vitals["bmi"] = float(vit["bmi"])
        except ValueError: pass
    if "height" in vit:
        vitals["height"] = vit["height"]
    if "temp" in vit:
        m = re.match(r"([\d.]+)", vit["temp"]); vitals["temp_f"] = float(m[1]) if m else None
    if "spo2" in vit:
        m = re.match(r"(\d+)", vit["spo2"]); vitals["spo2"] = int(m[1]) if m else None
    instr = []
    for l in block(L, lambda l: l == "Instructions", lambda l: l.startswith(SECTION_STARTS)):
        if l.startswith("from ") or l == "Instructions" or l in instr or BOILERPLATE.search(l) or not l.strip("\u200b\u00a0 "):
            continue
        instr.append(l)
    changes = block(L, lambda l: l == "Today's medication changes", lambda l: l.startswith(("Accurate as of", "Icon medication pick", "Review your")))
    changes = [c for c in changes if not c.startswith("Icon ")]
    labs = [l for l in block(L, lambda l: l in ("Labs ordered today", "Imaging ordered today", "Tests ordered today", "Referrals ordered today"))
            if re.match(r"^[A-Z0-9][A-Z0-9 ,/()&+\-'.]{3,}$", l)]
    procs = [l for l in block(L, lambda l: l == "Completed Today")]
    imaging = [l for l in L if re.match(r"^(XR|CT|MRI|MR|US|DEXA|DXA) ", l)]
    pickup = []
    for i, l in enumerate(L):
        if re.match(r"^(Pick these up at|Pick up these medications at)", l):
            for x in L[i + 1:i + 6]:
                if x.startswith(("Address", "Your estimated", "Phone")):
                    break
                pickup += [y.strip(" •") for y in x.split(" • ") if y.strip(" •")]
            break
    fu = next((f"{l} {L[i+1]}" if i + 1 < len(L) and L[i+1].startswith("(") else l for i, l in enumerate(L) if l.startswith("Return in about")), None)
    return {"file": name, "date": date, "time": time, "type": vtype, "clinician": clinician, "place": place,
            "issues": issues, "vitals": vitals, "instructions": instr, "med_changes": changes,
            "orders": labs, "refilled": pickup if vtype == "Refill" else [], "picked_up": pickup, "procedures": sorted(set(procs), key=procs.index), "imaging": sorted(set(imaging), key=imaging.index), "follow_up": fu}


def sources():
    if SRC.is_file() and SRC.suffix == ".zip":
        with zipfile.ZipFile(SRC) as z:
            for n in sorted(z.namelist()):
                if n.endswith(".html") and "Sutter_AVS_" in n:
                    yield Path(n).stem, z.read(n).decode("utf-8", "ignore")
    else:
        files = sorted(SRC.rglob("Sutter_AVS_*.html"))
        if not files:
            for zp in sorted(SRC.glob("*.zip")):
                with zipfile.ZipFile(zp) as z:
                    for n in sorted(z.namelist()):
                        if n.endswith(".html") and "Sutter_AVS_" in n:
                            yield Path(n).stem, z.read(n).decode("utf-8", "ignore")
                return
        for f in files:
            yield f.stem, f.read_text(encoding="utf-8", errors="ignore")


visits = [parse(n, raw) for n, raw in sources()]
visits.sort(key=lambda v: (v["date"], v["time"] or "", v["file"]))
OUT.write_text(json.dumps(visits, indent=1, ensure_ascii=False))
print(f"wrote {OUT}: {len(visits)} visits, {visits[0]['date']} to {visits[-1]['date']}" if visits else "no AVS files found")
