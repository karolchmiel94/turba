import asyncio
import json
import time
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class _Counters:
    submitted: int = 0
    confirmed: int = 0
    failed: int = 0
    confirm_ms: list = field(default_factory=list)
    fee_gwei: list = field(default_factory=list)
    api_ms: list = field(default_factory=list)


def _p(data: list, pct: int) -> float:
    if not data:
        return 0.0
    s = sorted(data)
    return s[max(0, int(len(s) * pct / 100) - 1)]


class Metrics:
    def __init__(self):
        self._nets: dict[str, _Counters] = defaultdict(_Counters)
        self._lock = asyncio.Lock()
        self._start = time.monotonic()

    async def submitted(self, net: str):
        async with self._lock:
            self._nets[net].submitted += 1

    async def confirmed(self, net: str, latency_ms: float):
        async with self._lock:
            self._nets[net].confirmed += 1
            self._nets[net].confirm_ms.append(latency_ms)

    async def failed(self, net: str):
        async with self._lock:
            self._nets[net].failed += 1

    async def fee(self, net: str, gwei: float):
        async with self._lock:
            self._nets[net].fee_gwei.append(gwei)

    async def api_latency(self, net: str, ms: float):
        async with self._lock:
            self._nets[net].api_ms.append(ms)

    def snapshot(self) -> dict:
        elapsed = round(time.monotonic() - self._start, 1)
        nets = {}
        for net, c in self._nets.items():
            nets[net] = {
                "submitted": c.submitted,
                "confirmed": c.confirmed,
                "failed": c.failed,
                "confirm_p50_ms": round(_p(c.confirm_ms, 50)),
                "confirm_p95_ms": round(_p(c.confirm_ms, 95)),
                "fee_p50_gwei": round(_p(c.fee_gwei, 50), 2),
                "fee_p95_gwei": round(_p(c.fee_gwei, 95), 2),
                "api_p50_ms": round(_p(c.api_ms, 50), 1),
                "api_p95_ms": round(_p(c.api_ms, 95), 1),
            }
        return {"elapsed_s": elapsed, "networks": nets}

    def print_summary(self):
        snap = self.snapshot()
        print(f"\n[t={snap['elapsed_s']}s]")
        for net, s in snap["networks"].items():
            print(
                f"  {net:6s}  submitted: {s['submitted']:4d}  "
                f"confirmed: {s['confirmed']:4d}  failed: {s['failed']:2d}\n"
                f"         confirm p50: {s['confirm_p50_ms']}ms   "
                f"p95: {s['confirm_p95_ms']}ms\n"
                f"         fee p50: {s['fee_p50_gwei']} gwei   "
                f"p95: {s['fee_p95_gwei']} gwei\n"
                f"         api latency p50: {s['api_p50_ms']}ms   "
                f"p95: {s['api_p95_ms']}ms"
            )

    def write_json(self, path: str):
        with open(path, "w") as f:
            json.dump(self.snapshot(), f, indent=2)
        print(f"Report written to {path}")

    async def periodic_report(self, stop: asyncio.Event, interval_s: int = 30):
        while not stop.is_set():
            await asyncio.sleep(interval_s)
            if not stop.is_set():
                self.print_summary()
