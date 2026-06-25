# Turba

Network traffic simulator for [Omnicatena](https://github.com/karolchmiel94/omnicatena). Spins up local blockchain nodes via Docker and drives them with configurable load — background noise directly into chain mempools plus simulated wallet users hitting the Omnicatena HTTP API.

## What it does

Two async layers run concurrently:

**Noise generator** — bypasses the Omnicatena API and floods mempools directly via chain RPCs. This is what actually drives EIP-1559 base fees up and backs up confirmation queues.

**User simulator** — exercises the Omnicatena API with realistic wallet lifecycles: create wallet → fund from dev account → submit transfer → poll until confirmed.

## Requirements

- Docker with Compose plugin
- Python 3.11+
- Omnicatena running at `http://localhost:8080` (unless overridden)

```bash
pip install -r requirements.txt
```

## Quick start

```bash
# Low-noise EVM run for 5 minutes
python simulate.py --networks eth,base --profile low --duration 300

# High congestion across all chains with a JSON report
python simulate.py --networks all --profile high --duration 600 --out report.json

# All nodes running, but only test Ethereum via the API
python simulate.py --networks all --test eth --profile medium --duration 120

# Noise only — no API calls (e.g. Omnicatena not yet running)
python simulate.py --networks eth,base --no-users --profile medium

# Skip Docker management if nodes are already running
python simulate.py --networks eth --no-docker --profile low
```

## Congestion profiles

| Parameter | `low` | `medium` | `high` |
|---|---|---|---|
| API users (concurrent) | 5 | 25 | 100 |
| Noise tx/s (per EVM network) | 0 | 15 | 60 |
| EVM block time | instant | 2 s | 12 s |
| BTC block interval | 10 s | 30 s | 120 s |

Override individual parameters at runtime:

```bash
python simulate.py --profile medium --users 50 --noise 30
```

## Supported networks

| Flag | Chain | Node |
|---|---|---|
| `btc` | Bitcoin | `bitcoin/bitcoin` (regtest) |
| `eth` | Ethereum | Foundry Anvil |
| `base` | Base | Foundry Anvil |
| `sol` | Solana | `solanalabs/solana` test-validator |
| `tron` | TRON | `trontools/quickstart` |

## CLI reference

```
usage: simulate.py [-h] [-n NETS] [-t NETS] [-p {low,medium,high}]
                   [-d SECONDS] [--users N] [--noise TPS]
                   [--api-url URL] [--no-noise] [--no-users] [--no-docker]
                   [--out FILE]

Options:
  -n, --networks NETS   Networks to start: comma-separated or "all" (default: all)
  -t, --test NETS       Networks to drive via Omnicatena API (default: same as --networks)
  -p, --profile         Congestion preset: low / medium / high (default: low)
  -d, --duration SEC    Run duration in seconds; omit to run until Ctrl+C
  --users N             Override concurrent API users from profile
  --noise TPS           Override noise tx/s per EVM network from profile
  --api-url URL         Omnicatena API base URL (default: http://localhost:8080)
  --no-noise            Skip noise generator
  --no-users            Skip user simulator
  --no-docker           Skip container management (assume nodes already up)
  --out FILE            Write final metrics to FILE as JSON
```

## Metrics

A live summary is printed every 30 seconds. A final report prints at exit:

```
[t=60s]  submitted: 847  confirmed: 791  failed: 3
         confirm p50: 14s   p95: 38s
         fee estimate p50: 12 gwei   p95: 31 gwei
         api latency p50: 8ms   p95: 22ms
```

Pass `--out report.json` to write the final summary as JSON.
