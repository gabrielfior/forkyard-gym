"""Thin client for one Forkyard session, plus a Forkyard process manager.

Talks only to the methods Forkyard's HTTP JSON-RPC surface actually implements (see
../forkyard/crates/api-http/src/lib.rs): eth_call, eth_getBalance, eth_getStorageAt, eth_getCode,
eth_getTransactionCount, eth_estimateGas, eth_gasPrice, eth_sendRawTransaction (legacy tx only),
eth_getTransactionReceipt, and the forkyard_setBalance / forkyard_setStorageAt cheatcodes. No
impersonation is available — every write is a real, locally-signed transaction from a key this
process holds, exactly like a real client would do against a real node.
"""
from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass

import httpx
from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_account import Account
from eth_account.datastructures import SignedTransaction
from eth_utils import function_signature_to_4byte_selector, to_checksum_address


def start_forkyard(rpc_url: str, port: int, cache_disabled: bool = True, log_path: str = "forkyard.log") -> subprocess.Popen:
    """Launches a local forkyard process, unpinned (the chain tip follower stays on) — each
    session pins its own block via POST /session's {"block_number": N} body instead, so one
    process serves every incident in this eval, each at its own historical block.

    stderr goes to a file (appended, not truncated) rather than a PIPE: a PIPE nobody drains fills
    its OS buffer and can wedge the child once it writes enough, and a crash's panic message is
    worth keeping around after the Python process that spawned it has moved on."""
    env = {**os.environ, "RPC_URL": rpc_url, "FORKYARD_PORT": str(port)}
    if cache_disabled:
        env["FORKYARD_CACHE_DISABLED"] = "1"
    log_f = open(log_path, "a")
    proc = subprocess.Popen(
        ["./target/release/forkyard"],
        cwd=os.path.expanduser("~/code/forkyard"),
        env=env,
        stdout=log_f,
        stderr=log_f,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"forkyard exited before starting; see {log_path}")
        try:
            r = httpx.post(f"{base}/session", json={}, timeout=2)
            if r.status_code == 200:
                return proc
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    proc.kill()
    raise RuntimeError("forkyard did not become ready in time")


def is_alive(proc: subprocess.Popen) -> bool:
    return proc.poll() is None


@dataclass
class Session:
    base: str
    session_id: int

    def rpc(self, method: str, params: list, timeout: float = 60) -> object:
        r = httpx.post(
            f"{self.base}/session/{self.session_id}",
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=timeout,
        )
        r.raise_for_status()
        out = r.json()
        if "error" in out:
            raise RuntimeError(f"{method}{params}: {out['error']}")
        return out["result"]

    def set_balance(self, address: str, wei: int) -> None:
        self.rpc("forkyard_setBalance", [address, hex(wei)])

    def get_balance(self, address: str) -> int:
        return int(self.rpc("eth_getBalance", [address, "latest"]), 16)

    def get_code_size(self, address: str) -> int:
        code = self.rpc("eth_getCode", [address, "latest"])
        return max(0, (len(code) - 2) // 2)

    def get_nonce(self, address: str) -> int:
        return int(self.rpc("eth_getTransactionCount", [address, "latest"]), 16)

    def gas_price(self) -> int:
        return int(self.rpc("eth_gasPrice", []), 16)

    def call(self, to: str, data: str) -> str:
        return self.rpc("eth_call", [{"to": to, "data": data}, "latest"])

    def estimate_gas(self, from_: str, to: str, data: str, value: int) -> int:
        try:
            return int(self.rpc("eth_estimateGas", [{"from": from_, "to": to, "data": data, "value": hex(value)}]), 16)
        except RuntimeError:
            return 500_000  # a generic fallback; the tx may still revert, which is itself informative

    def send_raw(self, raw_tx_hex: str) -> str:
        return self.rpc("eth_sendRawTransaction", [raw_tx_hex])

    def receipt(self, tx_hash: str, timeout: float = 20) -> dict | None:
        end = time.time() + timeout
        while time.time() < end:
            r = self.rpc("eth_getTransactionReceipt", [tx_hash], timeout=5)
            if r:
                return r
            time.sleep(0.3)
        return None


def open_session(base: str, block_number: int) -> Session:
    r = httpx.post(f"{base}/session", json={"block_number": block_number}, timeout=30)
    r.raise_for_status()
    return Session(base=base, session_id=r.json()["session_id"])


def encode_call(signature: str, args: list) -> str:
    """signature like 'balanceOf(address)' or 'transfer(address,uint256)'. args are plain Python
    values (str for address/bytes-as-hex, int for uint/int)."""
    name, _, rest = signature.partition("(")
    types = [t for t in rest.rstrip(")").split(",") if t]
    selector = function_signature_to_4byte_selector(signature)
    typed_args = [to_checksum_address(a) if t == "address" else int(a) for t, a in zip(types, args)]
    body = abi_encode(types, typed_args) if types else b""
    return "0x" + (selector + body).hex()


def decode_result(signature_return_types: list[str], data: str) -> list:
    if data in ("0x", "", None):
        return []
    return list(abi_decode(signature_return_types, bytes.fromhex(data[2:])))


def sign_and_build_raw(private_key: str, *, nonce: int, to: str | None, data: str, value: int,
                        gas: int, gas_price: int, chain_id: int = 1) -> SignedTransaction:
    tx = {
        "nonce": nonce,
        "to": to,
        "value": value,
        "gas": gas,
        "gasPrice": gas_price,
        "data": data,
        "chainId": chain_id,
    }
    return Account.sign_transaction(tx, private_key)


def new_account() -> tuple[str, str]:
    """Returns (private_key_hex, address) for a fresh, throwaway signing key."""
    acct = Account.create()
    return acct.key.hex(), acct.address
