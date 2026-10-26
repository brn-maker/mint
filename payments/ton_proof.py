import asyncio
import json
from pathlib import Path
from typing import Any

PROOF_SCRIPT = Path(__file__).with_name("ton_proof_verify.mjs")


class TonProofError(RuntimeError):
    pass


async def verify_ton_proof(
    *,
    address: str,
    public_key: str,
    proof: dict[str, Any],
    expected_domain: str,
    network: str,
) -> dict[str, Any]:
    payload = {
        "address": address,
        "public_key": public_key,
        "proof": proof,
        "expected_domain": expected_domain,
        "network": network,
    }
    try:
        process = await asyncio.create_subprocess_exec(
            "node",
            str(PROOF_SCRIPT),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(json.dumps(payload).encode("utf-8")),
                timeout=90,
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise
    except (OSError, asyncio.TimeoutError) as exc:
        raise TonProofError("TON proof verifier could not complete") from exc

    try:
        response = json.loads(stdout)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise TonProofError("TON proof verifier returned invalid output") from exc

    if not isinstance(response, dict) or "valid" not in response:
        raise TonProofError("TON proof verifier returned invalid output")
    return response
