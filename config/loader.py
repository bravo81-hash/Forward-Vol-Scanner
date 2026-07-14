from __future__ import annotations

from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _yaml(name: str) -> dict:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - installation error
        raise RuntimeError("PyYAML is required; install requirements.txt") from exc
    path = ROOT / name
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@lru_cache(maxsize=1)
def risk_config() -> dict:
    return _yaml("risk.yaml")


@lru_cache(maxsize=1)
def account_config() -> dict:
    return _yaml("accounts.yaml")


@lru_cache(maxsize=1)
def hypothesis_config() -> dict:
    cfg = _yaml("hypotheses.yaml")
    allowed = set(cfg.get("statuses", []))
    seen = set()
    for row in cfg.get("hypotheses", []):
        hid = row.get("id")
        if not hid or hid in seen:
            raise ValueError(f"duplicate or missing hypothesis id: {hid}")
        if row.get("status") not in allowed:
            raise ValueError(f"{hid}: invalid status {row.get('status')}")
        if not row.get("claim") or not row.get("metrics"):
            raise ValueError(f"{hid}: claim and metrics are required")
        seen.add(hid)
    return cfg


def account_profile(account: str | None, nlv: float | None = None) -> dict:
    cfg = account_config()
    profiles = cfg.get("accounts", {})
    p = dict(profiles.get(account or "", cfg.get("default", {})))
    p.update(account=account, nlv=float(nlv or p.get("nlv") or 100_000.0))
    p.setdefault("pool", "trading")
    p.setdefault("cash_account", False)
    p.setdefault("block_multi_expiry", False)
    p.setdefault("mode", "mock" if (account or "").startswith("MOCK") else "paper")
    return p


def hypothesis(hypothesis_id: str | None) -> dict:
    if not hypothesis_id:
        return {"id": None, "status": "HYPOTHESIS", "name": "unregistered"}
    for row in hypothesis_config().get("hypotheses", []):
        if row.get("id") == hypothesis_id:
            return row
    return {"id": hypothesis_id, "status": "HYPOTHESIS", "name": "unregistered"}
