#!/usr/bin/env python3
"""Sync BodySpec DEXA PDFs from Google Drive and rebuild /root/health-data/dexa/dexa_measurements.json.

- Finds BodySpec/DEXA PDFs visible to the Scout Google account (read-only Drive scope).
- Downloads new ones to raw/, extracts text with pdftotext -layout into text/.
- Parses every report in text/ and rewrites dexa_measurements.json (same schema as before).
Silent unless a new scan is added or something fails (so it can run from cron).
Use --dry-run to parse and print without writing; --no-drive to skip Drive.
"""
import json, re, subprocess, sys, urllib.parse, urllib.request
from pathlib import Path

ROOT = Path("/root/health-data/dexa")
RAW, TEXT, OUT = ROOT / "raw", ROOT / "text", ROOT / "dexa_measurements.json"
MANIFEST = ROOT / "drive_manifest.json"
# Google login for Drive (read-only use). The pipeline keeps its own copy; Scout's is the fallback.
TOKENS = [Path("/root/health-pipeline-home/google_token.json"), Path("/root/.hermes/profiles/personal/google_token.json")]
NUM = r"(-?\d[\d,]*\.?\d*)"


def mdy(s):
    m, d, y = s.split("/")
    return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"


def parse(text, src_txt, src_pdf):
    lines = text.splitlines()
    out = {"source_text": str(src_txt), "source_pdf": str(src_pdf)}
    # summary table: first data row after the "Total Body Fat %" header is this report's scan
    for i, l in enumerate(lines):
        if "Total Body Fat %" in l and "Measured Date" in l:
            for row in lines[i + 1:i + 8]:
                m = re.match(r"\s*(\d{1,2}/\d{1,2}/\d{4})\s+" + NUM + r"%\s+" + NUM + r"\s+" + NUM + r"\s+" + NUM + r"\s+" + NUM, row)
                if m:
                    out.update(date=mdy(m[1]), body_fat_pct=float(m[2]), total_mass_lb=float(m[3]), fat_tissue_lb=float(m[4]),
                               lean_tissue_lb=float(m[5]), bmc_lb=float(m[6]))
                    break
            break
    if "date" not in out:
        return None
    regions = {}
    for l in lines:
        m = re.match(r"\s*(Arms|Legs|Trunk|Android|Gynoid|Total)\s+" + NUM + r"%\s+" + NUM + r"\s+" + NUM + r"\s+" + NUM + r"\s+" + NUM + r"\s*$", l)
        if m and m[1].lower() not in regions:
            regions[m[1].lower()] = {"fat_pct": float(m[2]), "total_mass_lb": float(m[3]), "fat_tissue_lb": float(m[4]),
                                     "lean_tissue_lb": float(m[5]), "bmc_lb": float(m[6])}
    out["regions"] = regions
    for l in lines:
        m = re.match(r"\s*([\d,]+)\s*cal/day\s+" + NUM + r"%\s+" + NUM + r"%\s+" + NUM, l)
        if m:
            out.update(rmr_cal_day=int(m[1].replace(",", "")), android_fat_pct=float(m[2]), gynoid_fat_pct=float(m[3]), a_g_ratio=float(m[4]))
            break
    for l in lines:
        m = re.match(r"\s*Mass \(lbs\)\s+" + NUM, l)
        if m:
            out["vat_mass_lb"] = float(m[1])
            break
    return out


def drive_token():
    last = None
    for path in TOKENS:
        if not path.exists():
            continue
        t = json.loads(path.read_text())
        body = urllib.parse.urlencode({"client_id": t["client_id"], "client_secret": t["client_secret"],
                                       "refresh_token": t["refresh_token"], "grant_type": "refresh_token"}).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(t["token_uri"], data=body), timeout=30) as r:
                return json.loads(r.read())["access_token"]
        except Exception as e:  # expired/revoked login: try the next one
            last = e
    raise RuntimeError(f"no working Google login for Drive ({last})")


def drive_get(tok, url):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def drive_pdfs(tok):
    api = "https://www.googleapis.com/drive/v3/files"
    found = {}
    def q(query):
        params = urllib.parse.urlencode({"q": query, "fields": "files(id,name,modifiedTime,webViewLink,mimeType)", "pageSize": 200,
                                         "includeItemsFromAllDrives": "true", "supportsAllDrives": "true"})
        return json.loads(drive_get(tok, f"{api}?{params}")).get("files", [])
    folders = q("mimeType='application/vnd.google-apps.folder' and trashed=false and (name contains 'DEXA' or name contains 'Dexa' or name contains 'dexa' or name contains 'BodySpec')")
    for f in folders:
        for x in q(f"'{f['id']}' in parents and mimeType='application/pdf' and trashed=false"):
            found[x["id"]] = x
    for x in q("mimeType='application/pdf' and trashed=false and (name contains 'bodyspec' or name contains 'BodySpec')"):
        found[x["id"]] = x
    return list(found.values())


def main():
    dry, no_drive = "--dry-run" in sys.argv, "--no-drive" in sys.argv
    manifest = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}
    if OUT.exists():  # scans parsed before this script existed already have their Drive file id
        for s in json.loads(OUT.read_text()):
            if s.get("drive_file_id") and s["drive_file_id"] not in manifest:
                manifest[s["drive_file_id"]] = {"link": s.get("drive_link"), "txt": s.get("source_text"), "pdf": s.get("source_pdf"), "known": True}
    if not no_drive:
        tok = drive_token()
        for f in drive_pdfs(tok):
            if f["id"] in manifest:
                continue
            pdf = RAW / f"drive_{f['id']}.pdf"
            txt = TEXT / f"drive_{f['id']}.txt"
            if not dry:
                pdf.write_bytes(drive_get(tok, f"https://www.googleapis.com/drive/v3/files/{f['id']}?alt=media&supportsAllDrives=true"))
                subprocess.run(["pdftotext", "-layout", str(pdf), str(txt)], check=True)
                manifest[f["id"]] = {"name": f["name"], "link": f.get("webViewLink"), "pdf": str(pdf), "txt": str(txt), "modified": f.get("modifiedTime")}
            else:
                print("would download", f["name"])
    old = {s["date"]: s for s in json.loads(OUT.read_text())} if OUT.exists() else {}
    links = {v["txt"]: v.get("link") for v in manifest.values()}
    scans = {}
    for txt in sorted(TEXT.glob("*.txt")):
        pdf = RAW / (txt.stem + ".pdf")
        s = parse(txt.read_text(errors="ignore"), txt, pdf)
        if not s:
            continue
        prev = old.get(s["date"], {})
        if links.get(str(txt)):
            s["drive_link"] = links[str(txt)]
        if s["date"] not in scans:
            scans[s["date"]] = {**prev, **s, "regions": {**prev.get("regions", {}), **s["regions"]}}
    result = sorted(scans.values(), key=lambda s: s["date"])
    new = [s["date"] for s in result if s["date"] not in old]
    if dry:
        print(json.dumps([{k: s.get(k) for k in ("date", "body_fat_pct", "total_mass_lb", "fat_tissue_lb", "lean_tissue_lb", "bmc_lb", "rmr_cal_day", "a_g_ratio", "vat_mass_lb")} for s in result], indent=0))
        print("new:", new)
        return
    OUT.write_text(json.dumps(result, indent=2))
    MANIFEST.write_text(json.dumps(manifest, indent=2))
    if new:
        print("New DEXA scan added to the Health Portal: " + ", ".join(new))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"DEXA sync failed: {e}")
        sys.exit(1)
