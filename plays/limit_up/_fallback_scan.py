#!/usr/bin/env python3
"""
Fallback scanner for push2.eastmoney.com when HTTPS + proxy scan_surge() fails.

Place this in the maneki-agent project as plays/limit_up/_fallback_scan.py,
or run it standalone from /root/maneki-agent to generate a signal JSON file
that the pipeline can consume via --from-file.

Strategy: HTTP (non-HTTPS) direct to push2 with exponential backoff retries.
Bypasses TLS fingerprinting / WAF blocks that affect Python requests/curl to HTTPS.

Usage:
    cd /root/maneki-agent
    python plays/limit_up/_fallback_scan.py
    # Writes data/signals/surge_{ts}.json

Then feed to pipeline:
    python plays/limit_up/pipeline.py --from-file data/signals/surge_{ts}.json
"""
import requests
import json
import re
import time
import sys
from pathlib import Path


def scan_fallback(max_retries: int = 5, output_dir: str = "data/signals") -> list[dict]:
    """
    Scan Eastmoney clist API via HTTP (no proxy, no TLS) with exponential backoff.

    Returns list of candidate dicts: {code, name, pct_chg}
    Saves to {output_dir}/surge_{ts}.json as side effect.

    Returns empty list if all retries fail.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://quote.eastmoney.com/",
    }

    url = (
        "http://push2.eastmoney.com/api/qt/clist/get"
        "?np=1&fltt=2&invt=2"
        "&fs=m:0+t:6+f:!2,m:0+t:80+f:!2,m:1+t:2+f:!2,m:1+t:23+f:!2,"
        "m:0+t:81+s:262144+f:!2"
        "&fields=f12,f14,f2,f3,f11&pn=1&pz=200&po=1&dect=1"
        "&ut=fa5fd1943c7b386f172d6893dbfba10b&fid=f3"
    )

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, timeout=15, headers=headers)
            resp.encoding = "utf-8"
            data = resp.json()
            items = data.get("data", {}).get("diff", [])
            candidates = []
            for s in items:
                code = s.get("f12", "")
                name = s.get("f14", "")
                if not code or not name:
                    continue
                # Skip ST, *ST, 退市, 新股
                if re.search(r"ST|\*ST|退|N", name or ""):
                    continue
                # Skip 创业板(300), 科创板(688), 北交所(8/4/920)
                if re.match(r"^(300|301|688|8|4|920)", code):
                    continue
                pct = float(s.get("f3", 0) or 0)
                if pct < 2 or pct > 9.5:
                    continue
                if "." not in code:
                    code = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"
                candidates.append({"code": code, "name": name, "pct_chg": pct})

            if candidates:
                ts = time.strftime("%Y%m%d_%H%M", time.localtime())
                out_dir = Path(output_dir)
                out_dir.mkdir(parents=True, exist_ok=True)
                fname = out_dir / f"surge_{ts}.json"
                with open(fname, "w") as f:
                    json.dump(candidates, f, ensure_ascii=False)
                print(f"OK: {len(candidates)} candidates saved to {fname}")
                return candidates

            print(f"Attempt {attempt}: 0 candidates after filtering")
            time.sleep(2**attempt)

        except requests.exceptions.ConnectionError as e:
            print(f"Retry {attempt}: ConnectionError — {str(e)[:80]}")
            time.sleep(2**attempt)
        except json.JSONDecodeError as e:
            print(f"Retry {attempt}: Bad JSON — {str(e)[:80]}")
            time.sleep(2**attempt)
        except Exception as e:
            print(f"Retry {attempt}: {type(e).__name__} — {str(e)[:80]}")
            time.sleep(2**attempt)

    print("All retries failed — push2 completely unreachable")
    return []


if __name__ == "__main__":
    result = scan_fallback()
    if result:
        top_10 = [c["code"] for c in result[:10]]
        print(f"Top candidates: {json.dumps(top_10)}")
    else:
        print("No candidates found.")
        sys.exit(1)
