"""地址校验与 EOA/proxy 解析。"""

from __future__ import annotations

import re
from typing import Any

try:
    from Crypto.Hash import keccak  # type: ignore
except ImportError:  # pragma: no cover - 见 sha3 兜底
    keccak = None

import hashlib

ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def is_address(value: str) -> bool:
    """是否为合法 EVM 地址。"""
    return bool(ADDRESS_RE.match(value.strip()))


def normalize(address: str) -> str:
    """地址归一为小写并去空白。"""
    return address.strip().lower()


def _keccak256(data: bytes) -> bytes:
    """keccak256（优先 pycryptodome，缺失时用 hashlib sha3_256 近似——注意二者不同）。"""
    if keccak is not None:
        k = keccak.new(digest_bits=256)
        k.update(data)
        return k.digest()
    # hashlib.sh3_256 是标准 SHA3，与 keccak 不同；仅在未装 pycryptodome 时兜底，
    # 派生结果会在联调（TBD-03）中验证，默认配置不依赖该路径。
    return hashlib.sha3_256(data).digest()


def compute_create2(factory: str, salt: bytes, init_code: bytes) -> str:
    """按 EIP-1014 计算 CREATE2 地址。"""
    raw = b"\xff" + bytes.fromhex(factory[2:]) + salt + _keccak256(init_code)
    return "0x" + _keccak256(raw)[-20:].hex()


def derive_safe_proxy(eoa: str, factory: str, proxy_init_code: str) -> str | None:
    """用 EOA 作为 salt 派生 Gnosis Safe 代理钱包。

    与 market 官方工厂"以 EOA 为 salt"的约定一致。init_code 为空时返回 None。
    """
    if not proxy_init_code.strip():
        return None
    init_code = bytes.fromhex(proxy_init_code.strip().removeprefix("0x"))
    salt = _keccak256(bytes.fromhex(eoa[2:]))
    return compute_create2(factory, salt, init_code)


async def is_contract(http_post: Any, address: str) -> bool | None:
    """通过 eth_getCode 判断地址是否为已部署合约；无法判断时返回 None。"""
    try:
        result = await http_post(
            "eth_getCode", [address, "latest"]
        )
    except Exception:  # noqa: BLE001 - 派生验证是增强能力，失败不影响主流程
        return None
    if isinstance(result, str):
        return result not in ("0x", "0x0")
    return None
