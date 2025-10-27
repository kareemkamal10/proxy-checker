import asyncio
import aiohttp
import argparse
import time
import pandas as pd
from aiohttp import ClientTimeout

async def check_proxy(session, proxy, timeout, max_latency):
    test_url = "https://httpbin.org/ip"
    start = time.time()
    try:
        async with session.get(test_url, proxy=f"http://{proxy}", timeout=timeout) as response:
            if response.status == 200:
                latency = time.time() - start
                if latency <= max_latency:
                    return proxy, latency, True
    except:
        pass
    return proxy, None, False

async def run(proxy_list, max_latency):
    timeout = ClientTimeout(total=max_latency + 3)
    async with aiohttp.ClientSession() as session:
        tasks = [check_proxy(session, proxy, timeout, max_latency) for proxy in proxy_list]
        return await asyncio.gather(*tasks)

def load_proxies_from_urls(urls):
    proxies = []
    import requests
    for url in urls:
        try:
            res = requests.get(url, timeout=10)
            if res.status_code == 200:
                for line in res.text.splitlines():
                    if ":" in line:
                        proxies.append(line.strip())
        except:
            pass
    return proxies

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--urls", nargs="+", required=True)
    parser.add_argument("--out", default="proxies_all.csv")
    parser.add_argument("--out-good", default="proxies_good.txt")
    parser.add_argument("--max-latency", type=float, default=3.0)
    args = parser.parse_args()

    print("[+] Loading proxy lists...")
    proxies = load_proxies_from_urls(args.urls)
    print(f"[+] Loaded {len(proxies)} proxies")

    results = asyncio.run(run(proxies, args.max_latency))

    all_data = []
    good = []
    for proxy, latency, ok in results:
        all_data.append([proxy, latency, ok])
        if ok:
            good.append(proxy)

    df = pd.DataFrame(all_data, columns=["proxy", "latency", "ok"])
    df.to_csv(args.out, index=False)

    with open(args.out_good, "w") as f:
        for g in good:
            f.write(g + "\n")

    print(f"[+] Done! Good proxies: {len(good)} ✅")
    print(f"[+] Saved all proxies → {args.out}")
    print(f"[+] Saved working proxies → {args.out_good}")
