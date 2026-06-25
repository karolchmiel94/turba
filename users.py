"""
User simulator: drives Omnicatena HTTP API through a realistic wallet lifecycle
per tested network. Funding wallets uses chain RPC directly (not the API).

Expected API shape (adjust as the real API solidifies):
  POST /wallets                  → {id, accounts: {<net>: {address}}}
  POST /transactions             → {hash, fee_gwei}
  GET  /transactions/{hash}      → {status: "pending"|"confirmed"|"failed"}
"""

import asyncio
import time

import httpx

from metrics import Metrics

# Chain-specific amounts for the test transfer
_AMOUNTS = {
    "eth":  "0.001",
    "base": "0.001",
    "btc":  "0.0005",
    "sol":  "0.01",
    "tron": "1",
}

_POLL_INTERVAL = 2   # seconds between confirmation polls
_POLL_TIMEOUT  = 300 # seconds before giving up on a tx


async def _fund_evm(address: str, rpc_url: str, chain_id: int):
    from web3 import AsyncWeb3
    w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(rpc_url))
    funder = w3.eth.account.from_key(
        "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
    )
    nonce = await w3.eth.get_transaction_count(funder.address)
    gas_price = await w3.eth.gas_price
    tx = {
        "to": address,
        "value": w3.to_wei(0.01, "ether"),
        "gas": 21000,
        "maxFeePerGas": gas_price,
        "maxPriorityFeePerGas": 1_000_000_000,
        "nonce": nonce,
        "chainId": chain_id,
        "type": 2,
    }
    signed = funder.sign_transaction(tx)
    await w3.eth.send_raw_transaction(signed.raw_transaction)


async def _fund_btc(address: str):
    import concurrent.futures
    from bitcoinrpc.authproxy import AuthServiceProxy

    def _send():
        rpc = AuthServiceProxy("http://omni:omni@localhost:18443")
        try:
            rpc.loadwallet("turba-users")
        except Exception:
            try:
                rpc.createwallet("turba-users")
            except Exception:
                pass
        # Mine some blocks if no balance
        rpc.sendtoaddress(address, 0.001)

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _send)


async def _fund_sol(address: str):
    import httpx
    async with httpx.AsyncClient(timeout=10) as c:
        await c.post("http://localhost:8899", json={
            "jsonrpc": "2.0", "id": 1,
            "method": "requestAirdrop",
            "params": [address, 1_000_000_000],
        })
    await asyncio.sleep(1)


async def _fund_tron(address: str):
    from tronpy import AsyncTron
    from tronpy.keys import PrivateKey
    from tronpy.providers import AsyncHTTPProvider

    key = PrivateKey(bytes.fromhex(
        "da146374a75310b9666e834ee4ad0866d6f4035967bfc76217c5a495fff9f0d0"
    ))
    sender = key.public_key.to_base58check_address()
    client = AsyncTron(provider=AsyncHTTPProvider("http://localhost:9090"))
    try:
        txb = await client.trx.transfer(sender, address, 10_000_000)
        txb = txb.build()
        await txb.sign(key).broadcast()
        await asyncio.sleep(3)
    finally:
        await client.close()


_FUND = {
    "eth":  lambda addr: _fund_evm(addr, "http://localhost:8545", 31337),
    "base": lambda addr: _fund_evm(addr, "http://localhost:8546", 8453),
    "btc":  _fund_btc,
    "sol":  _fund_sol,
    "tron": _fund_tron,
}


class UserSimulator:
    def __init__(
        self,
        api_url: str,
        networks: set,
        concurrent: int,
        metrics: Metrics,
    ):
        self.api_url = api_url.rstrip("/")
        self.networks = list(networks)
        self.concurrent = concurrent
        self.metrics = metrics

    async def run(self, stop: asyncio.Event):
        # Divide concurrent users evenly across tested networks
        per_net = max(1, self.concurrent // len(self.networks))
        workers = [
            asyncio.create_task(self._worker(net, stop))
            for net in self.networks
            for _ in range(per_net)
        ]
        await asyncio.gather(*workers, return_exceptions=True)

    async def _worker(self, net: str, stop: asyncio.Event):
        async with httpx.AsyncClient(base_url=self.api_url, timeout=30) as client:
            while not stop.is_set():
                try:
                    await self._scenario(client, net)
                except Exception:
                    await self.metrics.failed(net)
                    await asyncio.sleep(1)

    async def _scenario(self, client: httpx.AsyncClient, net: str):
        # 1. Create sender wallet
        t0 = time.monotonic()
        r = await client.post("/wallets", json={"name": f"turba-{net}-{time.time_ns()}"})
        await self.metrics.api_latency(net, (time.monotonic() - t0) * 1000)
        r.raise_for_status()
        sender = r.json()
        sender_addr = sender["accounts"][net]["address"]

        # 2. Create receiver wallet
        t0 = time.monotonic()
        r = await client.post("/wallets", json={"name": f"turba-recv-{net}-{time.time_ns()}"})
        await self.metrics.api_latency(net, (time.monotonic() - t0) * 1000)
        r.raise_for_status()
        receiver_addr = r.json()["accounts"][net]["address"]

        # 3. Fund sender via chain RPC
        await _FUND[net](sender_addr)

        # 4. Submit transfer
        t0 = time.monotonic()
        r = await client.post("/transactions", json={
            "network": net,
            "from_wallet_id": sender["id"],
            "to_address": receiver_addr,
            "amount": _AMOUNTS[net],
        })
        await self.metrics.api_latency(net, (time.monotonic() - t0) * 1000)
        r.raise_for_status()
        tx = r.json()
        tx_hash = tx["hash"]
        if fee := tx.get("fee_gwei"):
            await self.metrics.fee(net, float(fee))

        await self.metrics.submitted(net)

        # 5. Poll until confirmed
        submit_time = time.monotonic()
        while True:
            if time.monotonic() - submit_time > _POLL_TIMEOUT:
                await self.metrics.failed(net)
                return
            await asyncio.sleep(_POLL_INTERVAL)
            t0 = time.monotonic()
            r = await client.get(f"/transactions/{tx_hash}", params={"network": net})
            await self.metrics.api_latency(net, (time.monotonic() - t0) * 1000)
            r.raise_for_status()
            status = r.json().get("status")
            if status == "confirmed":
                latency_ms = (time.monotonic() - submit_time) * 1000
                await self.metrics.confirmed(net, latency_ms)
                return
            if status == "failed":
                await self.metrics.failed(net)
                return
