import random
from decimal import Decimal


NETWORK_UPTIME_PERCENT = Decimal("99.98")

SERVER_POOLS: dict[str, dict[str, object]] = {
    "NVIDIA-H100-POOL-A": {
        "label": "H100 Tensor Core Cluster",
        "gpu": "NVIDIA H100 SXM",
        "gpu_count": 64,
        "annual_yield": Decimal("0.24"),
        "tenant_workloads": [
            "LLM inference · 8 tenants",
            "Model fine-tuning · 3 tenants",
            "Scientific computing · 2 tenants",
        ],
    },
    "NVIDIA-A100-POOL-B": {
        "label": "A100 Compute Cluster",
        "gpu": "NVIDIA A100 80GB",
        "gpu_count": 128,
        "annual_yield": Decimal("0.18"),
        "tenant_workloads": [
            "Computer vision · 12 tenants",
            "Batch inference · 9 tenants",
            "Data processing · 5 tenants",
        ],
    },
}


def generate_diagnostics(server_pool: str) -> dict[str, object]:
    pool = SERVER_POOLS.get(server_pool)
    if pool is None:
        raise ValueError(f"Unknown server pool: {server_pool}")

    workloads = list(pool["tenant_workloads"])
    random.shuffle(workloads)
    return {
        "core_temperature_celsius": round(random.uniform(62, 68), 1),
        "load_percent": random.randint(78, 96),
        "network_uptime_percent": float(NETWORK_UPTIME_PERCENT),
        "running_tenant_workloads": workloads[: random.randint(1, len(workloads))],
    }


def format_commission_event(username: str, amount: Decimal, currency: str) -> str:
    handle = username.lstrip("@") if username else "anon" + str(random.randint(100, 999))
    formatted_amount = f"{amount:,.0f}" if amount == amount.to_integral_value() else f"{amount:,.2f}"
    return f"⚡ User @{handle} just banked {formatted_amount} {currency} in upfront commissions!"
