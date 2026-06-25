#!/usr/bin/env python3
import argparse
import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from metrics import Metrics
from noise import create_noise_gens
from profiles import ALL_NETWORKS, NETWORK_SERVICE, parse_networks, resolve
from users import UserSimulator

COMPOSE = Path(__file__).parent / "docker-compose.yml"

# (host, port) per network for readiness probing
_PORTS = {
    "btc":  ("localhost", 18443),
    "eth":  ("localhost", 8545),
    "base": ("localhost", 8546),
    "sol":  ("localhost", 8899),
    "tron": ("localhost", 9090),
}


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def _compose(args: list[str], env: dict | None = None):
    merged = {**os.environ, **(env or {})}
    subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE)] + args,
        env=merged,
        check=True,
    )


def start_networks(networks: set, profile):
    services = [NETWORK_SERVICE[n] for n in networks]
    env = {"EVM_BLOCK_TIME": str(profile.evm_block_time)}
    print(f"Starting containers: {', '.join(services)}")
    _compose(["up", "-d"] + services, env=env)


def stop_networks():
    print("Stopping containers...")
    _compose(["down"])


def wait_ready(networks: set, timeout_s: int = 60):
    print("Waiting for nodes to be ready...", end="", flush=True)
    deadline = time.monotonic() + timeout_s
    remaining = set(networks)
    while remaining and time.monotonic() < deadline:
        for net in list(remaining):
            host, port = _PORTS[net]
            if _port_open(host, port):
                remaining.discard(net)
        if remaining:
            print(".", end="", flush=True)
            time.sleep(2)
    print()
    if remaining:
        print(f"Timed out waiting for: {', '.join(sorted(remaining))}", file=sys.stderr)
        sys.exit(1)
    print("All nodes ready.")


async def _run(args, profile, start_nets: set, test_nets: set):
    metrics = Metrics()
    stop = asyncio.Event()

    def _signal_handler():
        print("\nInterrupted, stopping...")
        stop.set()

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, _signal_handler)
    loop.add_signal_handler(signal.SIGTERM, _signal_handler)

    tasks = []

    if not args.no_noise:
        noise_gens = create_noise_gens(start_nets, profile)
        for net, gen in noise_gens.items():
            tasks.append(asyncio.create_task(
                gen.run(profile.noise_tps, stop), name=f"noise-{net}"
            ))

    if not args.no_users and test_nets:
        sim = UserSimulator(args.api_url, test_nets, profile.users, metrics)
        tasks.append(asyncio.create_task(sim.run(stop), name="users"))

    tasks.append(asyncio.create_task(
        metrics.periodic_report(stop, interval_s=30), name="reporter"
    ))

    if args.duration:
        async def _timer():
            await asyncio.sleep(args.duration)
            stop.set()
        tasks.append(asyncio.create_task(_timer(), name="timer"))
        print(f"Running for {args.duration}s (Ctrl+C to stop early)")
    else:
        print("Running indefinitely (Ctrl+C to stop)")

    await asyncio.gather(*tasks, return_exceptions=True)

    print("\n--- Final report ---")
    metrics.print_summary()
    if args.out:
        metrics.write_json(args.out)


def main():
    parser = argparse.ArgumentParser(
        description="Turba — network traffic simulator for Omnicatena",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Networks: btc, eth, base, sol, tron  (or "all")

Examples:
  # Quiet EVM run for 5 minutes
  python simulate.py --networks eth,base --profile low --duration 300

  # High congestion, all chains, JSON report
  python simulate.py --networks all --profile high --duration 600 --out report.json

  # Start all nodes but only test Ethereum via Omnicatena API
  python simulate.py --networks all --test eth --profile medium --duration 120

  # Noise only — no API test (e.g. before Omnicatena is running)
  python simulate.py --networks eth,base --no-users --profile medium
""",
    )

    parser.add_argument(
        "-n", "--networks",
        default="all",
        metavar="NETS",
        help="Networks to start (comma-separated or 'all'). Default: all",
    )
    parser.add_argument(
        "-t", "--test",
        default=None,
        metavar="NETS",
        help="Networks to drive via Omnicatena API (default: same as --networks)",
    )
    parser.add_argument(
        "-p", "--profile",
        choices=["low", "medium", "high"],
        default="low",
        help="Congestion preset. Default: low",
    )
    parser.add_argument(
        "-d", "--duration",
        type=int,
        default=None,
        metavar="SECONDS",
        help="Run duration in seconds. Omit to run until Ctrl+C",
    )
    parser.add_argument(
        "--users",
        type=int,
        default=None,
        metavar="N",
        help="Override concurrent API users from profile",
    )
    parser.add_argument(
        "--noise",
        type=int,
        default=None,
        metavar="TPS",
        help="Override noise tx/s per EVM network from profile",
    )
    parser.add_argument(
        "--api-url",
        default="http://localhost:8080",
        metavar="URL",
        help="Omnicatena HTTP API base URL. Default: http://localhost:8080",
    )
    parser.add_argument(
        "--no-noise",
        action="store_true",
        help="Skip noise generator (only run user simulator)",
    )
    parser.add_argument(
        "--no-users",
        action="store_true",
        help="Skip user simulator (only run noise generator)",
    )
    parser.add_argument(
        "--no-docker",
        action="store_true",
        help="Skip container management (nodes already running)",
    )
    parser.add_argument(
        "--out",
        metavar="FILE",
        help="Write final metrics to FILE as JSON",
    )

    args = parser.parse_args()

    try:
        start_nets = parse_networks(args.networks)
        test_nets = parse_networks(args.test) if args.test else set(start_nets)
    except ValueError as e:
        parser.error(str(e))

    if not test_nets.issubset(start_nets):
        unknown = test_nets - start_nets
        parser.error(
            f"--test networks not in --networks: {', '.join(sorted(unknown))}. "
            f"Either add them to --networks or remove from --test."
        )

    profile = resolve(args.profile, args.users, args.noise)

    print(
        f"Profile: {args.profile}  |  "
        f"users: {profile.users}  noise: {profile.noise_tps} tps  "
        f"evm-block-time: {profile.evm_block_time}s\n"
        f"Networks (start): {', '.join(sorted(start_nets))}\n"
        f"Networks (test):  {', '.join(sorted(test_nets)) if not args.no_users else 'none (--no-users)'}"
    )

    if not args.no_docker:
        start_networks(start_nets, profile)
        wait_ready(start_nets)

    try:
        asyncio.run(_run(args, profile, start_nets, test_nets))
    finally:
        if not args.no_docker:
            stop_networks()


if __name__ == "__main__":
    main()
