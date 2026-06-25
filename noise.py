import asyncio
import time
from abc import ABC, abstractmethod

# Anvil's well-known funded dev account private keys (accounts[0..4])
_ANVIL_KEYS = [
    "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80",
    "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d",
    "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a",
    "0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6",
    "0x47e179ec197488593b187f80a00eb0da91f1b9d0b13f8733639f19c30a34926b",
]

# trontools/quickstart pre-funded account (account[0])
_TRON_FUNDED_KEY = "da146374a75310b9666e834ee4ad0866d6f4035967bfc76217c5a495fff9f0d0"


class NoiseGen(ABC):
    @abstractmethod
    async def run(self, tps: int, stop: asyncio.Event): ...


class EVMNoiseGen(NoiseGen):
    def __init__(self, rpc_url: str, chain_id: int):
        self.rpc_url = rpc_url
        self.chain_id = chain_id

    async def run(self, tps: int, stop: asyncio.Event):
        if tps == 0:
            return
        from web3 import AsyncWeb3

        w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(self.rpc_url))
        accounts = [w3.eth.account.from_key(k) for k in _ANVIL_KEYS]
        nonces = {}
        for a in accounts:
            nonces[a.address] = await w3.eth.get_transaction_count(a.address)

        interval = 1.0 / tps
        idx = 0
        while not stop.is_set():
            t0 = time.monotonic()
            sender = accounts[idx % len(accounts)]
            receiver = accounts[(idx + 1) % len(accounts)]
            nonce = nonces[sender.address]
            nonces[sender.address] += 1
            try:
                gas_price = await w3.eth.gas_price
                tx = {
                    "to": receiver.address,
                    "value": 1,
                    "gas": 21000,
                    "maxFeePerGas": gas_price,
                    "maxPriorityFeePerGas": 1_000_000_000,
                    "nonce": nonce,
                    "chainId": self.chain_id,
                    "type": 2,
                }
                signed = sender.sign_transaction(tx)
                await w3.eth.send_raw_transaction(signed.raw_transaction)
            except Exception:
                # Nonce may have drifted; resync on next iteration
                nonces[sender.address] = await w3.eth.get_transaction_count(sender.address)
            idx += 1
            elapsed = time.monotonic() - t0
            if elapsed < interval:
                await asyncio.sleep(interval - elapsed)


class BTCNoiseGen(NoiseGen):
    """Mines regtest blocks at btc_block_interval and spams the mempool."""

    def __init__(self, rpc_url: str, block_interval_s: int):
        self.rpc_url = rpc_url
        self.block_interval_s = block_interval_s

    async def run(self, tps: int, stop: asyncio.Event):
        import concurrent.futures
        from bitcoinrpc.authproxy import AuthServiceProxy

        loop = asyncio.get_event_loop()
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

        def _mine_loop():
            rpc = AuthServiceProxy(self.rpc_url)
            try:
                rpc.createwallet("turba-noise")
            except Exception:
                rpc.loadwallet("turba-noise")
            addr = rpc.getnewaddress()
            # Mine 101 blocks so coinbase coins mature
            rpc.generatetoaddress(101, addr)
            while not stop.is_set():
                # Spam a few cheap mempool txs before each block
                for _ in range(5):
                    try:
                        dest = rpc.getnewaddress()
                        rpc.sendtoaddress(dest, 0.0001)
                    except Exception:
                        pass
                rpc.generatetoaddress(1, addr)
                time.sleep(self.block_interval_s)

        await loop.run_in_executor(executor, _mine_loop)


class SolanaNoiseGen(NoiseGen):
    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url

    async def run(self, tps: int, stop: asyncio.Event):
        if tps == 0:
            return
        import base64
        import httpx
        from solders.hash import Hash
        from solders.keypair import Keypair
        from solders.message import Message
        from solders.system_program import TransferParams, transfer
        from solders.transaction import Transaction

        payer = Keypair()
        receiver = Keypair()

        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(self.rpc_url, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "requestAirdrop",
                "params": [str(payer.pubkey()), 1_000_000_000_000],
            })
            await asyncio.sleep(2)

            interval = 1.0 / tps
            while not stop.is_set():
                t0 = time.monotonic()
                try:
                    r = await client.post(self.rpc_url, json={
                        "jsonrpc": "2.0", "id": 1,
                        "method": "getLatestBlockhash",
                        "params": [{"commitment": "finalized"}],
                    })
                    bh = Hash.from_string(r.json()["result"]["value"]["blockhash"])
                    ix = transfer(TransferParams(
                        from_pubkey=payer.pubkey(),
                        to_pubkey=receiver.pubkey(),
                        lamports=1000,
                    ))
                    msg = Message.new_with_blockhash([ix], payer.pubkey(), bh)
                    tx = Transaction([payer], msg, bh)
                    encoded = base64.b64encode(bytes(tx)).decode()
                    await client.post(self.rpc_url, json={
                        "jsonrpc": "2.0", "id": 1,
                        "method": "sendTransaction",
                        "params": [encoded, {"encoding": "base64"}],
                    })
                except Exception:
                    pass
                elapsed = time.monotonic() - t0
                if elapsed < interval:
                    await asyncio.sleep(interval - elapsed)


class TRONNoiseGen(NoiseGen):
    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url

    async def run(self, tps: int, stop: asyncio.Event):
        if tps == 0:
            return
        from tronpy import AsyncTron
        from tronpy.keys import PrivateKey
        from tronpy.providers import AsyncHTTPProvider

        key = PrivateKey(bytes.fromhex(_TRON_FUNDED_KEY))
        sender = key.public_key.to_base58check_address()
        client = AsyncTron(provider=AsyncHTTPProvider(self.rpc_url))
        interval = 1.0 / tps

        # Derive a stable receiver from a second hardcoded key
        recv_key = PrivateKey(bytes.fromhex(
            "cdb5f6f9e4a925e7a3dc0b902cdad1c892c64de85b17cfbd5e83b6e89a03a055"
        ))
        receiver = recv_key.public_key.to_base58check_address()

        try:
            while not stop.is_set():
                t0 = time.monotonic()
                try:
                    txb = await client.trx.transfer(sender, receiver, 1_000_000)
                    txb = txb.build()
                    txn = txb.sign(key)
                    await txn.broadcast()
                except Exception:
                    pass
                elapsed = time.monotonic() - t0
                if elapsed < interval:
                    await asyncio.sleep(interval - elapsed)
        finally:
            await client.close()


def create_noise_gens(networks: set, profile) -> dict:
    gens = {}
    if "eth" in networks:
        gens["eth"] = EVMNoiseGen("http://localhost:8545", 31337)
    if "base" in networks:
        gens["base"] = EVMNoiseGen("http://localhost:8546", 8453)
    if "btc" in networks:
        gens["btc"] = BTCNoiseGen(
            "http://omni:omni@localhost:18443",
            profile.btc_block_interval,
        )
    if "sol" in networks:
        gens["sol"] = SolanaNoiseGen("http://localhost:8899")
    if "tron" in networks:
        gens["tron"] = TRONNoiseGen("http://localhost:9090")
    return gens
