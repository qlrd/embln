"""BOLT 1/2 message constants and pure parse/build helpers (no LightningNode dep)."""

import os

# message type IDs
OPEN_CHANNEL_TYPE = 32
ACCEPT_CHANNEL_TYPE = 33

# regtest genesis (display order, matches getblockchaininfo at height 0)
REGTEST_CHAIN_HASH = bytes.fromhex(
    "0f9188f13cb7b2c71f2a335e3a4fc328bf5beb436012afca590b1a11466e2206"
)

# Mandatory-fields-only message lengths (no TLV). Used by the server-side reader
# to know how many bytes to consume per type.
MSG_LENGTH = {
    OPEN_CHANNEL_TYPE: 321,
    ACCEPT_CHANNEL_TYPE: 272,
}


def _u(buf: bytes, offset: int, n: int) -> tuple[int, int]:
    return int.from_bytes(buf[offset : offset + n], "big"), offset + n


def _b(buf: bytes, offset: int, n: int) -> tuple[bytes, int]:
    return buf[offset : offset + n], offset + n


def build_open_channel(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    basepoints: dict,
    funding_satoshis: int = 100_000_000,
    push_msat: int = 0,
    dust_limit_satoshis: int = 546,
    max_htlc_value_in_flight_msat: int = None,
    channel_reserve_satoshis: int = None,
    htlc_minimum_msat: int = 1000,
    feerate_per_kw: int = 250,
    to_self_delay: int = 144,
    max_accepted_htlcs: int = 483,
    channel_flags: int = 0,
    chain_hash: bytes = REGTEST_CHAIN_HASH,
    temporary_channel_id: bytes = None,
) -> bytes:
    """BOLT 2 open_channel wire bytes (mandatory fields only, no TLVs).

    `max_htlc_value_in_flight_msat` and `channel_reserve_satoshis` default to
    the standard derivations from `funding_satoshis` when not given.
    """
    if temporary_channel_id is None:
        temporary_channel_id = os.urandom(32)
    if max_htlc_value_in_flight_msat is None:
        max_htlc_value_in_flight_msat = funding_satoshis * 1000
    if channel_reserve_satoshis is None:
        channel_reserve_satoshis = max(funding_satoshis // 100, dust_limit_satoshis)

    body = (
        chain_hash
        + temporary_channel_id
        + funding_satoshis.to_bytes(8, "big")
        + push_msat.to_bytes(8, "big")
        + dust_limit_satoshis.to_bytes(8, "big")
        + max_htlc_value_in_flight_msat.to_bytes(8, "big")
        + channel_reserve_satoshis.to_bytes(8, "big")
        + htlc_minimum_msat.to_bytes(8, "big")
        + feerate_per_kw.to_bytes(4, "big")
        + to_self_delay.to_bytes(2, "big")
        + max_accepted_htlcs.to_bytes(2, "big")
        + basepoints["funding_pubkey"]
        + basepoints["revocation_basepoint"]
        + basepoints["payment_basepoint"]
        + basepoints["delayed_payment_basepoint"]
        + basepoints["htlc_basepoint"]
        + basepoints["first_per_commitment_point"]
        + bytes([channel_flags])
    )
    return OPEN_CHANNEL_TYPE.to_bytes(2, "big") + body


def build_accept_channel(
    basepoints: dict,
    open_channel: dict,
    minimum_depth: int = 3,
) -> bytes:
    """BOLT 2 accept_channel wire bytes — echoes policy from `open_channel` and
    contributes our own basepoints (mandatory fields only, no TLVs)."""
    body = (
        open_channel["temporary_channel_id"]
        + open_channel["dust_limit_satoshis"].to_bytes(8, "big")
        + open_channel["max_htlc_value_in_flight_msat"].to_bytes(8, "big")
        + open_channel["channel_reserve_satoshis"].to_bytes(8, "big")
        + open_channel["htlc_minimum_msat"].to_bytes(8, "big")
        + minimum_depth.to_bytes(4, "big")
        + open_channel["to_self_delay"].to_bytes(2, "big")
        + open_channel["max_accepted_htlcs"].to_bytes(2, "big")
        + basepoints["funding_pubkey"]
        + basepoints["revocation_basepoint"]
        + basepoints["payment_basepoint"]
        + basepoints["delayed_payment_basepoint"]
        + basepoints["htlc_basepoint"]
        + basepoints["first_per_commitment_point"]
    )
    return ACCEPT_CHANNEL_TYPE.to_bytes(2, "big") + body


def parse_open_channel(msg: bytes) -> dict:
    """Decode a BOLT 2 open_channel wire message (mandatory fields, no TLV)."""
    msg_type, off = _u(msg, 0, 2)
    if msg_type != OPEN_CHANNEL_TYPE:
        raise ValueError(
            f"expected open_channel type {OPEN_CHANNEL_TYPE}, got {msg_type}"
        )
    chain_hash, off = _b(msg, off, 32)
    temporary_channel_id, off = _b(msg, off, 32)
    funding_satoshis, off = _u(msg, off, 8)
    push_msat, off = _u(msg, off, 8)
    dust_limit_satoshis, off = _u(msg, off, 8)
    max_htlc_value_in_flight_msat, off = _u(msg, off, 8)
    channel_reserve_satoshis, off = _u(msg, off, 8)
    htlc_minimum_msat, off = _u(msg, off, 8)
    feerate_per_kw, off = _u(msg, off, 4)
    to_self_delay, off = _u(msg, off, 2)
    max_accepted_htlcs, off = _u(msg, off, 2)
    funding_pubkey, off = _b(msg, off, 33)
    revocation_basepoint, off = _b(msg, off, 33)
    payment_basepoint, off = _b(msg, off, 33)
    delayed_payment_basepoint, off = _b(msg, off, 33)
    htlc_basepoint, off = _b(msg, off, 33)
    first_per_commitment_point, off = _b(msg, off, 33)
    channel_flags, off = _u(msg, off, 1)
    return {
        "type": msg_type,
        "chain_hash": chain_hash,
        "temporary_channel_id": temporary_channel_id,
        "funding_satoshis": funding_satoshis,
        "push_msat": push_msat,
        "dust_limit_satoshis": dust_limit_satoshis,
        "max_htlc_value_in_flight_msat": max_htlc_value_in_flight_msat,
        "channel_reserve_satoshis": channel_reserve_satoshis,
        "htlc_minimum_msat": htlc_minimum_msat,
        "feerate_per_kw": feerate_per_kw,
        "to_self_delay": to_self_delay,
        "max_accepted_htlcs": max_accepted_htlcs,
        "funding_pubkey": funding_pubkey,
        "revocation_basepoint": revocation_basepoint,
        "payment_basepoint": payment_basepoint,
        "delayed_payment_basepoint": delayed_payment_basepoint,
        "htlc_basepoint": htlc_basepoint,
        "first_per_commitment_point": first_per_commitment_point,
        "channel_flags": channel_flags,
    }


def parse_accept_channel(msg: bytes) -> dict:
    """Decode a BOLT 2 accept_channel wire message (mandatory fields, no TLV)."""
    msg_type, off = _u(msg, 0, 2)
    if msg_type != ACCEPT_CHANNEL_TYPE:
        raise ValueError(
            f"expected accept_channel type {ACCEPT_CHANNEL_TYPE}, got {msg_type}"
        )
    temporary_channel_id, off = _b(msg, off, 32)
    dust_limit_satoshis, off = _u(msg, off, 8)
    max_htlc_value_in_flight_msat, off = _u(msg, off, 8)
    channel_reserve_satoshis, off = _u(msg, off, 8)
    htlc_minimum_msat, off = _u(msg, off, 8)
    minimum_depth, off = _u(msg, off, 4)
    to_self_delay, off = _u(msg, off, 2)
    max_accepted_htlcs, off = _u(msg, off, 2)
    funding_pubkey, off = _b(msg, off, 33)
    revocation_basepoint, off = _b(msg, off, 33)
    payment_basepoint, off = _b(msg, off, 33)
    delayed_payment_basepoint, off = _b(msg, off, 33)
    htlc_basepoint, off = _b(msg, off, 33)
    first_per_commitment_point, off = _b(msg, off, 33)
    return {
        "type": msg_type,
        "temporary_channel_id": temporary_channel_id,
        "dust_limit_satoshis": dust_limit_satoshis,
        "max_htlc_value_in_flight_msat": max_htlc_value_in_flight_msat,
        "channel_reserve_satoshis": channel_reserve_satoshis,
        "htlc_minimum_msat": htlc_minimum_msat,
        "minimum_depth": minimum_depth,
        "to_self_delay": to_self_delay,
        "max_accepted_htlcs": max_accepted_htlcs,
        "funding_pubkey": funding_pubkey,
        "revocation_basepoint": revocation_basepoint,
        "payment_basepoint": payment_basepoint,
        "delayed_payment_basepoint": delayed_payment_basepoint,
        "htlc_basepoint": htlc_basepoint,
        "first_per_commitment_point": first_per_commitment_point,
    }
