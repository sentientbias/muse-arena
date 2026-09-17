#!/usr/bin/env python3
"""x402 v2 payment handling for Muse Arena's stdlib HTTP server.

Muse Arena runs on http.server (not FastAPI), so the x402 SDK's FastAPI
payment middleware cannot be used here. Instead this module drives the SAME
x402 v2 protocol with the SDK's own sync primitives:

    unpaid request  -> 402 + PAYMENT-REQUIRED header (payment requirements)
    paid request    -> decode X-Payment / PAYMENT-SIGNATURE header
                    -> facilitator.verify()  (EIP-3009 authorization check)
                    -> facilitator.settle()  (USDC transferWithAuthorization)
                    -> 200 + PAYMENT-RESPONSE header (settlement receipt)

This is the exact pattern used by x402-seller/server.py (402 challenge ->
signed EIP-3009 -> verify -> settle via the USDC contract), minus the
FastAPI middleware wrapper.

Mainnet settlement requires the CDP facilitator, which needs CDP_API_KEY_ID
and CDP_API_KEY_SECRET (same env vars as x402-seller). Without them the
public x402.org facilitator is used, which only supports testnet -- so
mainnet stake requests fail closed with 503 instead of taking money that
cannot be settled.

Stakes: exactly $1.00 USDC (1,000,000 base units) on Base mainnet,
paid to the mission wallet.
"""
import os

# --- sandbox proxy fix -----------------------------------------------------
# This VM exports NO_PROXY with bracketed IPv6 entries (e.g. "[::1]") that
# httpx 0.28 cannot parse ("Invalid port: ':1]'"), which crashes the x402
# SDK's facilitator client at request time. Scope the fix to this process
# only: keep the real proxy vars, simplify no_proxy to plain IPv4 hosts.
os.environ["no_proxy"] = os.environ["NO_PROXY"] = "localhost,127.0.0.1"
# ---------------------------------------------------------------------------

import json

NETWORK = os.environ.get("X402_STAKE_NETWORK", "eip155:8453")  # Base mainnet
PAY_TO = os.environ.get(
    "X402_STAKE_PAY_TO",
    "0xCe668A6eEd09dC1b53D6b231c4875456668C1775",  # mission wallet (house)
).strip()
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
STAKE_PRICE = "$1.00"
STAKE_UNITS = 1_000_000  # $1.00 USDC in 6-decimal base units
FACILITATOR_URL = os.environ.get("X402_FACILITATOR_URL", "https://x402.org/facilitator")
MAX_TIMEOUT_SECONDS = 300

try:
    from x402.http import HTTPFacilitatorClientSync, FacilitatorConfig
    from x402.http.constants import (
        PAYMENT_REQUIRED_HEADER,
        PAYMENT_RESPONSE_HEADER,
        PAYMENT_SIGNATURE_HEADER,
        X_PAYMENT_HEADER,
    )
    from x402.http.utils import (
        decode_payment_required_header,
        decode_payment_signature_header,
        encode_payment_required_header,
        encode_payment_response_header,
    )
    from x402.mechanisms.evm.exact import ExactEvmServerScheme
    from x402.payment_flow import apply_payment_flow_wire_extra, resolve_payment_flow
    from x402.schemas import PaymentRequirements, SupportedKind
    from x402.schemas.payments import PaymentRequired
    HAVE_X402 = True
except ImportError:
    HAVE_X402 = False


class StakePayError(Exception):
    """User-facing payment failure (message is safe to return in a 402)."""


_facilitator = None
_requirements = None
_exact_scheme = None


def _facilitator_config():
    """CDP facilitator with API-key auth when keys are present (mainnet),
    otherwise the public x402.org facilitator (testnet only). Mirrors
    x402-seller/server.py."""
    key_id = os.environ.get("CDP_API_KEY_ID", "").strip()
    key_secret = os.environ.get("CDP_API_KEY_SECRET", "").strip()
    if key_id and key_secret:
        from cdp.x402 import create_facilitator_config

        return create_facilitator_config(key_id, key_secret)
    return FacilitatorConfig(url=FACILITATOR_URL)


def mainnet_ready():
    """True when mainnet settlement is possible (CDP keys configured)."""
    if NETWORK != "eip155:8453":
        return True  # testnet: public facilitator suffices
    return bool(
        os.environ.get("CDP_API_KEY_ID", "").strip()
        and os.environ.get("CDP_API_KEY_SECRET", "").strip()
    )


# Test hook: tests may inject a fake facilitator with .verify/.settle.
FACILITATOR_OVERRIDE = None


def get_facilitator():
    global _facilitator
    if FACILITATOR_OVERRIDE is not None:
        return FACILITATOR_OVERRIDE
    if _facilitator is None:
        if not HAVE_X402:
            raise StakePayError("x402 SDK not installed — staking disabled")
        _facilitator = HTTPFacilitatorClientSync(_facilitator_config())
    return _facilitator


def stake_requirements():
    """Build the $1.00 USDC payment requirements (cached). Same construction
    the SDK's build_payment_requirements() performs, minus the facilitator
    /supported round-trip (which only lists testnet on the public
    facilitator)."""
    global _requirements, _exact_scheme
    if _requirements is None:
        if not HAVE_X402:
            raise StakePayError("x402 SDK not installed — staking disabled")
        _exact_scheme = ExactEvmServerScheme()
        asset_amount = _exact_scheme.parse_price(STAKE_PRICE, NETWORK)
        req = PaymentRequirements(
            scheme="exact",
            network=NETWORK,
            asset=asset_amount.asset,
            amount=asset_amount.amount,
            pay_to=PAY_TO,
            max_timeout_seconds=MAX_TIMEOUT_SECONDS,
            extra=dict(asset_amount.extra or {}),
        )
        kind = SupportedKind(x402_version=2, scheme="exact", network=NETWORK)
        req = _exact_scheme.enhance_payment_requirements(req, kind, [])
        resolved = resolve_payment_flow(_exact_scheme, req)
        req.extra = apply_payment_flow_wire_extra(dict(req.extra or {}), resolved)
        if req.amount != str(STAKE_UNITS):
            raise StakePayError("stake price misconfiguration")
        _requirements = req
    return _requirements


def challenge():
    """Return (headers, body) for the 402 payment challenge."""
    req = stake_requirements()
    pr = PaymentRequired(x402_version=2, error=None, resource=None, accepts=[req])
    headers = {
        PAYMENT_REQUIRED_HEADER: encode_payment_required_header(pr),
        "Cache-Control": "no-store",
    }
    body = {
        "error": "payment required",
        "price_usd": "1.00",
        "price_units": STAKE_UNITS,
        "network": NETWORK,
        "asset": "USDC",
        "asset_contract": USDC_BASE,
        "pay_to": PAY_TO,
        "how_to": (
            "Sign an x402 v2 EIP-3009 USDC authorization for exactly "
            f"{STAKE_UNITS} base units ($1.00) to {PAY_TO} on {NETWORK} "
            "and resend this request with the PAYMENT-SIGNATURE header "
            "(X-Payment also accepted)."
        ),
    }
    return headers, body


def _decode_payment_header(value):
    try:
        return decode_payment_signature_header(value)
    except Exception as e:
        raise StakePayError(f"unreadable payment header: {e}")


def settle_stake_payment(payment_header_value):
    """Verify + settle a stake payment. Returns a receipt dict.

    Raises StakePayError with a user-facing message on any failure.
    On success the $1.00 USDC has moved onchain to the mission wallet.
    """
    req = stake_requirements()
    payload = _decode_payment_header(payment_header_value)

    # Sanity: the payment must actually target THESE requirements.
    try:
        accepted = payload.accepted
    except Exception:
        raise StakePayError("payment payload missing accepted requirements")
    if accepted.scheme != "exact" or accepted.network != NETWORK:
        raise StakePayError("payment is for a different scheme/network")
    if accepted.pay_to.lower() != PAY_TO.lower():
        raise StakePayError("payment is not addressed to the stake wallet")
    if accepted.amount != str(STAKE_UNITS):
        raise StakePayError("payment amount is not exactly $1.00 USDC")

    facilitator = get_facilitator()
    try:
        verified = facilitator.verify(payload, req)
    except Exception as e:
        raise StakePayError(f"payment verification failed: {e}")
    if not getattr(verified, "is_valid", False):
        reason = getattr(verified, "invalid_message", None) or getattr(
            verified, "invalid_reason", "unknown"
        )
        raise StakePayError(f"payment invalid: {reason}")

    try:
        settled = facilitator.settle(payload, req)
    except Exception as e:
        raise StakePayError(f"payment settlement failed: {e}")
    if not getattr(settled, "success", False):
        reason = getattr(settled, "error_message", None) or getattr(
            settled, "error_reason", "unknown"
        )
        raise StakePayError(f"settlement failed: {reason}")

    tx_hash = getattr(settled, "transaction", "") or ""
    payer = getattr(settled, "payer", "") or ""
    # Fall back to the authorization's `from` when the facilitator omits payer.
    if not payer:
        try:
            payer = payload.payload["authorization"]["from"]
        except Exception:
            payer = ""
    receipt = {
        "tx_hash": tx_hash,
        "payer": payer,
        "amount_units": STAKE_UNITS,
        "amount_usd": "1.00",
        "network": NETWORK,
    }
    headers = {}
    if HAVE_X402:
        try:
            headers[PAYMENT_RESPONSE_HEADER] = encode_payment_response_header(settled)
        except Exception:
            pass
    return receipt, headers


def reset_cache():
    """Test helper: drop cached facilitator/requirements."""
    global _facilitator, _requirements, _exact_scheme
    _facilitator, _requirements, _exact_scheme = None, None, None
