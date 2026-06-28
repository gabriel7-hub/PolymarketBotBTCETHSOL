#!/usr/bin/env python3
"""
Latency checker for the Polymarket migration (Bangalore → London A/B).

Measures three layers, lowest-to-highest signal:
  1. HTTP round-trip to the CLOB REST + Gamma hosts (the ORDER-PLACEMENT path) — DNS/connect/TLS/TTFB.
  2. CLOB WebSocket PING→PONG round-trip (the BOOK-FEED path) — the engine replies "PONG" to
     {"type":"PING"}, so this times a real round-trip through Cloudflare to Polymarket's WS infra
     and back. This is the number that tracks how stale your local book is.
  3. (separately) realized FILL slippage via analyze_fills.py — the only ground-truth P&L metric.

NOTE: clob.polymarket.com is Cloudflare-fronted, so HTTP/WS RTT partly reflects your nearest
Cloudflare edge, not the origin matching engine. The honest A/B is: run this on the OLD host and
the NEW (London) host and compare, then confirm with analyze_fills before/after.

    python3 check_latency.py                 # default 20 WS pings, all hosts
    python3 check_latency.py --pings 50
"""
import argparse, json, socket, ssl, statistics, time
from urllib.parse import urlparse
import http.client

REST_HOSTS = ["clob.polymarket.com", "gamma-api.polymarket.com"]
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def http_timing(host, path="/", samples=8):
    """DNS / TCP connect / TLS / TTFB breakdown (ms), median over samples."""
    dns, conn, tls, ttfb = [], [], [], []
    for _ in range(samples):
        try:
            t0 = time.perf_counter()
            ip = socket.gethostbyname(host)
            t1 = time.perf_counter()
            sock = socket.create_connection((ip, 443), timeout=5)
            t2 = time.perf_counter()
            ctx = ssl.create_default_context()
            ss = ctx.wrap_socket(sock, server_hostname=host)
            t3 = time.perf_counter()
            req = f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n"
            ss.sendall(req.encode())
            ss.recv(1)
            t4 = time.perf_counter()
            ss.close()
            dns.append((t1 - t0) * 1e3); conn.append((t2 - t1) * 1e3)
            tls.append((t3 - t2) * 1e3); ttfb.append((t4 - t2) * 1e3)
        except Exception as e:
            print(f"  {host}: error {e}")
            return
    md = lambda x: statistics.median(x)
    print(f"  {host:32} dns={md(dns):6.1f}  tcp_connect={md(conn):6.1f}  "
          f"tls_handshake={md(tls):6.1f}  ttfb_after_connect={md(ttfb):6.1f}  (ms, median)")


def origin_rtt(host, path, samples=10):
    """TTFB on a tiny DYNAMIC endpoint over a kept-alive connection — reuses one TLS session so the
    number is ~pure request→origin→response. The closer this is to your TCP-connect RTT, the closer
    your VPS is to Polymarket's origin matching engine (London edge ≈2ms but origin TTFB ≈98ms ⇒
    origin is ~96ms away, i.e. US-East — a US VPS would cut this further)."""
    try:
        ip = socket.gethostbyname(host)
        ctx = ssl.create_default_context()
        conn = http.client.HTTPSConnection(host, 443, timeout=5, context=ctx)
        conn.connect()
        rtts = []
        for _ in range(samples):
            s = time.perf_counter()
            conn.request("GET", path)
            r = conn.getresponse(); r.read()
            rtts.append((time.perf_counter() - s) * 1e3)
        conn.close()
        rtts.sort()
        print(f"  {host}{path:10} min={rtts[0]:6.1f}  median={statistics.median(rtts):6.1f}  "
              f"max={rtts[-1]:6.1f}  (ms) — origin matching-engine round-trip")
    except Exception as e:
        print(f"  {host}{path}: error {e}")


def ws_ping_rtt(url, n=20):
    """Connect to the CLOB market socket, send {"type":"PING"} n times, time each PONG."""
    try:
        from websocket import create_connection  # websocket-client
    except ImportError:
        print("  (pip install websocket-client to measure WS RTT)")
        return
    try:
        t0 = time.perf_counter()
        ws = create_connection(url, timeout=10)
        ws.settimeout(3.0)
        connect_ms = (time.perf_counter() - t0) * 1e3
    except Exception as e:
        print(f"  WS connect error: {e}")
        return
    rtts = []
    try:
        for _ in range(n):
            s = time.perf_counter()
            ws.send(json.dumps({"type": "PING"}))
            # read until we see PONG or the per-ping budget elapses (the socket may push
            # unrelated frames between our PING and its PONG)
            got = False
            while time.perf_counter() - s < 3.0:
                try:
                    msg = ws.recv()
                except Exception:
                    break
                if "PONG" in str(msg).upper():
                    rtts.append((time.perf_counter() - s) * 1e3)
                    got = True
                    break
            if not got and not rtts:
                # socket doesn't echo PONG idle — stop early, fall back to connect RTT
                break
            time.sleep(0.25)
    finally:
        ws.close()
    print(f"  WS connect: {connect_ms:.1f} ms")
    if rtts:
        rtts.sort()
        p95 = rtts[min(len(rtts) - 1, int(0.95 * len(rtts)))]
        print(f"  WS PING→PONG RTT (n={len(rtts)}): "
              f"min={rtts[0]:.1f}  median={statistics.median(rtts):.1f}  "
              f"p95={p95:.1f}  max={rtts[-1]:.1f}  (ms)")
    else:
        print("  WS PING→PONG: no PONG received (engine may not echo on this socket)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pings", type=int, default=20, help="WS PING samples")
    args = ap.parse_args()
    print(f"Latency check @ {time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n")
    print("HTTP round-trip (order-placement path):")
    for h in REST_HOSTS:
        http_timing(h)
    print("\nOrigin RTT (DYNAMIC endpoint, not edge-cached — isolates distance to the matching engine):")
    origin_rtt("clob.polymarket.com", "/time")
    print("\nWebSocket round-trip (book-feed path):")
    ws_ping_rtt(WS_URL, args.pings)
    print("\nNext: compare these on the OLD vs NEW host, then confirm with "
          "`python3 analyze_fills.py --db bot_state.db` before/after (the real P&L metric).")


if __name__ == "__main__":
    main()
