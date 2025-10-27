#!/usr/bin/env python3 """ proxy_collector.py

Purpose:

Download free proxy lists from configurable sources (raw .txt/.csv/.json URLs)

Deduplicate proxies (IP:PORT)

Run multi-stage checks:

1. TCP connect check (fast)


2. HTTP(S) proxy functional test (via httpbin.org/ip)



(Optional) Enrich results with ip-api (isp/org/country) for simple ASN/ISP heuristics

Export results as CSV and a simple IP:PORT text file for "good" proxies.


Usage:

Edit the PROXY_SOURCES list or pass a file with URLs

Run: python proxy_collector.py --out results.csv


Notes:

Designed to be run in environments like Colab or a Linux runner.

Keep an eye on rate-limits for ip-api (free). You can disable ASN lookup.


"""

import argparse import asyncio import aiohttp import async_timeout import csv import json import re import time from typing import List, Dict, Optional

----------------- CONFIG -----------------

PROXY_SOURCES_DEFAULT = [ "https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=5000&country=all&ssl=all&anonymity=all", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/proxy-list-raw.txt", "https://www.proxy-list.download/api/v1/get?type=http", "https://raw.githubusercontent.com/proxifly/free-proxy-list/master/proxies.txt", ] IPPORT_RE = re.compile(r"(\d{1,3}(?:.\d{1,3}){3}):(\d{1,5})") TEST_URL = "https://httpbin.org/ip" CONNECT_TIMEOUT = 6.0 CONCURRENT_FETCH = 6 CONCURRENT_TCP = 500 CONCURRENT_HTTP = 200 IPAPI_URL = "http://ip-api.com/json/"

------------------------------------------

async def fetch_text(session: aiohttp.ClientSession, url: str) -> str: try: async with session.get(url, timeout=10) as r: return await r.text() except Exception as e: print(f"[fetch error] {url} -> {e}") return ""

async def gather_proxies(urls: List[str]) -> List[str]: connector = aiohttp.TCPConnector(limit=CONCURRENT_FETCH, ssl=False) async with aiohttp.ClientSession(connector=connector) as session: tasks = [fetch_text(session, u) for u in urls] results = await asyncio.gather(*tasks) found = [] for txt in results: for m in IPPORT_RE.finditer(txt): ip, pr = m.groups() p = int(pr) if 1 <= p <= 65535: found.append(f"{ip}:{p}") # dedupe preserve order seen = set(); out = [] for p in found: if p not in seen: seen.add(p); out.append(p) return out

async def tcp_check(ip: str, port: int, timeout: float = CONNECT_TIMEOUT) -> (bool, Optional[float]): start = time.perf_counter() try: fut = asyncio.open_connection(ip, port) reader, writer = await asyncio.wait_for(fut, timeout=timeout) elapsed = time.perf_counter() - start try: writer.close() await writer.wait_closed() except Exception: pass return True, elapsed except Exception: return False, None

async def bulk_tcp_test(proxies: List[str], timeout: float = CONNECT_TIMEOUT) -> List[Dict]: sem = asyncio.Semaphore(CONCURRENT_TCP) results = [] async def _test(p): async with sem: ip, pr = p.split(":") ok, lat = await tcp_check(ip, int(pr), timeout=timeout) return {"proxy": p, "tcp_ok": ok, "tcp_latency": lat} tasks = [asyncio.create_task(_test(p)) for p in proxies] for t in asyncio.as_completed(tasks): res = await t results.append(res) print(f"[tcp-tested] {res['proxy']} ok={res['tcp_ok']} lat={res['tcp_latency']}") return results

async def test_http_proxy(proxy: str, timeout: float = CONNECT_TIMEOUT) -> Dict: proxy_url = f"http://{proxy}" start = time.perf_counter() try: timeout_cfg = aiohttp.ClientTimeout(total=timeout) async with aiohttp.ClientSession(timeout=timeout_cfg) as sess: async with sess.get(TEST_URL, proxy=proxy_url) as resp: text = await resp.text() elapsed = time.perf_counter() - start origin = "" try: origin = json.loads(text).get("origin", "") except Exception: origin = "" # optional header introspection try: async with sess.get("https://httpbin.org/headers", proxy=proxy_url) as hresp: headers_text = await hresp.text() headers_json = json.loads(headers_text) headers = headers_json.get("headers", {}) except Exception: headers = {} return {"proxy": proxy, "http_ok": True, "http_latency": elapsed, "origin": origin, "headers": headers} except Exception as e: return {"proxy": proxy, "http_ok": False, "http_latency": None, "origin": "", "error": str(e)}

async def bulk_http_test(proxies: List[str]) -> List[Dict]: sem = asyncio.Semaphore(CONCURRENT_HTTP) results = [] async def _test(p): async with sem: r = await test_http_proxy(p) print(f"[http-tested] {p} -> ok={r.get('http_ok')} lat={r.get('http_latency')}") return r tasks = [asyncio.create_task(_test(p)) for p in proxies] for t in asyncio.as_completed(tasks): res = await t results.append(res) return results

async def enrich_ipapi(rows: List[Dict]): connector = aiohttp.TCPConnector(limit=10, ssl=False) async with aiohttp.ClientSession(connector=connector) as session: for r in rows: ip = r["proxy"].split(":")[0] try: async with session.get(IPAPI_URL + ip, timeout=6) as resp: if resp.status == 200: j = await resp.json() r["isp"] = j.get("isp", "") r["org"] = j.get("org", "") r["country"] = j.get("country", "") else: r["isp"] = r["org"] = r["country"] = "" except Exception: r["isp"] = r["org"] = r["country"] = "" await asyncio.sleep(0.15)  # be polite to free API

def save_csv(rows: List[Dict], out_path: str): if not rows: print("[save] no rows") return # determine fieldnames fieldnames = set() for r in rows: fieldnames.update(r.keys()) fieldnames = list(fieldnames) with open(out_path, "w", newline='', encoding='utf-8') as f: writer = csv.DictWriter(f, fieldnames=fieldnames) writer.writeheader() for r in rows: writer.writerow(r) print(f"[saved] {out_path} ({len(rows)} rows)")

async def main_async(args): sources = args.urls or PROXY_SOURCES_DEFAULT print("[phase] fetching lists...") proxies = await gather_proxies(sources) print(f"[info] found {len(proxies)} unique proxies")

# quick TCP sweep to drop closed ports
print("[phase] tcp checking...")
tcp_rows = await bulk_tcp_test(proxies)
ok_tcp = [r['proxy'] for r in tcp_rows if r.get('tcp_ok')]
print(f"[info] tcp ok count: {len(ok_tcp)}")

# HTTP test only for those that passed TCP
print("[phase] http proxy checking (this will do real requests via proxy)...")
http_rows = await bulk_http_test(ok_tcp)

# merge tcp + http rows
merged = []
tcp_map = {r['proxy']: r for r in tcp_rows}
for hr in http_rows:
    proxy = hr['proxy']
    tr = tcp_map.get(proxy, {})
    row = {**tr, **hr}
    merged.append(row)

# optional ip-api enrichment
if args.do_asn:
    print("[phase] enriching with ip-api (may be rate-limited)...")
    await enrich_ipapi(merged)

# sort by latency
for r in merged:
    r['_lat_sort'] = r.get('http_latency') if r.get('http_latency') is not None else 9999.0
merged.sort(key=lambda x: x['_lat_sort'])

# filter
filtered = []
for r in merged:
    if args.only_ok and not r.get('http_ok'):
        continue
    if args.max_latency is not None:
        lat = r.get('http_latency')
        if lat is None or lat > args.max_latency:
            continue
    filtered.append(r)

save_csv(merged, args.out)
if args.out_good:
    with open(args.out_good, 'w', encoding='utf-8') as f:
        for r in filtered:
            f.write(r['proxy'] + "\n")
    print(f"[saved] good proxies -> {args.out_good} ({len(filtered)} rows)")

print("[done]")

def parse_args(): p = argparse.ArgumentParser(description="Collect & test free proxies") p.add_argument('--urls', nargs='+', help='One or more URLs with proxy lists (raw text)') p.add_argument('--out', default='proxies_all.csv', help='CSV output for all tested proxies') p.add_argument('--out-good', default='proxies_good.txt', help='Output text file with good proxies (IP:PORT)') p.add_argument('--only-ok', action='store_true', help='Only include http_ok==True in good file') p.add_argument('--max-latency', type=float, default=None, help='Max http latency (seconds) to include in good file') p.add_argument('--do-asn', action='store_true', help='Do ip-api enrichment (may be rate limited)') return p.parse_args()

def main(): args = parse_args() try: asyncio.run(main_async(args)) except KeyboardInterrupt: print('Interrupted')

if name == 'main': main()
