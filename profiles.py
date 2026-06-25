from dataclasses import dataclass

ALL_NETWORKS = {"btc", "eth", "base", "sol", "tron"}

# Maps turba network names to docker-compose service names
NETWORK_SERVICE = {
    "btc":  "bitcoind",
    "eth":  "ethereum",
    "base": "base",
    "sol":  "solana",
    "tron": "tron",
}


@dataclass
class Profile:
    users: int           # total concurrent API users across all tested networks
    noise_tps: int       # noise transactions per second per EVM network; scaled down for others
    evm_block_time: int  # seconds; 0 = instant (anvil mines on each tx)
    btc_block_interval: int  # seconds between mined regtest blocks


PROFILES = {
    "low":    Profile(users=5,   noise_tps=0,  evm_block_time=0,  btc_block_interval=10),
    "medium": Profile(users=25,  noise_tps=15, evm_block_time=2,  btc_block_interval=30),
    "high":   Profile(users=100, noise_tps=60, evm_block_time=12, btc_block_interval=120),
}


def resolve(profile_name: str, users_override=None, noise_override=None) -> Profile:
    base = PROFILES[profile_name]
    return Profile(
        users=users_override if users_override is not None else base.users,
        noise_tps=noise_override if noise_override is not None else base.noise_tps,
        evm_block_time=base.evm_block_time,
        btc_block_interval=base.btc_block_interval,
    )


def parse_networks(val: str) -> set:
    if val.strip().lower() == "all":
        return set(ALL_NETWORKS)
    nets = {n.strip().lower() for n in val.split(",")}
    unknown = nets - ALL_NETWORKS
    if unknown:
        raise ValueError(
            f"Unknown networks: {', '.join(sorted(unknown))}. "
            f"Valid: {', '.join(sorted(ALL_NETWORKS))}"
        )
    return nets
