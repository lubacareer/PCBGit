"""Strict, read-only availability checks for a KiCad CSV bill of materials.

Credentials are taken only from environment variables. Responses and exception
messages from remote services are never included in reports.
"""
from __future__ import annotations

import csv
import json
import os
import re
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

_TIMEOUT = 15
_MAX_RESPONSE = 2_000_000


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _json_request(url: str, *, data: bytes | None = None, headers: dict | None = None) -> dict:
    request = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.build_opener(_NoRedirect()).open(request, timeout=_TIMEOUT) as response:
        data = response.read(_MAX_RESPONSE + 1)
    if len(data) > _MAX_RESPONSE:
        raise ValueError("response too large")
    result = json.loads(data)
    if not isinstance(result, dict):
        raise ValueError("invalid response")
    return result


def _digikey(sku: str) -> tuple[int | None, str]:
    client_id = os.environ.get("DIGIKEY_CLIENT_ID", "")
    secret = os.environ.get("DIGIKEY_CLIENT_SECRET", "")
    token = os.environ.get("DIGIKEY_ACCESS_TOKEN", "")
    if not client_id or not (token or secret):
        return None, "DigiKey credentials are missing"
    try:
        if not token:
            payload = urllib.parse.urlencode({"client_id": client_id, "client_secret": secret, "grant_type": "client_credentials"}).encode()
            token = _json_request("https://api.digikey.com/v1/oauth2/token", data=payload, headers={"Content-Type": "application/x-www-form-urlencoded"})["access_token"]
        headers = {"Authorization": "Bearer " + token, "X-DIGIKEY-Client-Id": client_id}
        if os.environ.get("DIGIKEY_ACCOUNT_ID"):
            headers["X-DIGIKEY-Account-Id"] = os.environ["DIGIKEY_ACCOUNT_ID"]
        url = "https://api.digikey.com/products/v4/search/" + urllib.parse.quote(sku, safe="") + "/productdetails"
        product = _json_request(url, headers=headers)["Product"]
        matches = [p for p in product.get("ProductVariations", []) if str(p.get("DigiKeyProductNumber", "")).casefold() == sku.casefold()]
        if len(matches) != 1:
            return None, "DigiKey returned no unique exact SKU match"
        quantity = matches[0].get("QuantityAvailableforPackageType")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0:
            return None, "DigiKey did not provide a usable package quantity"
        return quantity, "exact DigiKey package match"
    except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError):
        return None, "DigiKey lookup failed or returned an invalid response"


def _mouser(sku: str) -> tuple[int | None, str]:
    key = os.environ.get("MOUSER_API_KEY", "")
    if not key:
        return None, "Mouser credentials are missing"
    # The endpoint accepts a pipe-delimited list, but we check one SKU at a
    # time; do not allow a BOM field to change the scope of the query.
    if not 3 <= len(sku) <= 40 or "|" in sku:
        return None, "Mouser SKU must be a single part number of 3 to 40 characters"
    try:
        url = "https://api.mouser.com/api/v1/search/partnumber?" + urllib.parse.urlencode({"apiKey": key})
        body = json.dumps({"SearchByPartRequest": {"mouserPartNumber": sku, "partSearchOptions": "Exact"}}).encode()
        response = _json_request(url, data=body, headers={"Content-Type": "application/json"})
        if response.get("Errors"):
            return None, "Mouser returned an API error"
        matches = [p for p in response["SearchResults"]["Parts"] if str(p.get("MouserPartNumber", "")).casefold() == sku.casefold()]
        if len(matches) != 1:
            return None, "Mouser returned no unique exact SKU match"
        # AvailabilityInStock is a separate API response field. Some API
        # responses only provide the human-readable Availability string.
        stock = matches[0].get("AvailabilityInStock")
        quantity = str(stock).strip() if stock is not None else ""
        digits = r"(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)"
        if quantity and re.fullmatch(digits, quantity):
            return int(quantity.replace(",", "")), "exact Mouser SKU match"
        availability = str(matches[0].get("Availability", "")).strip()
        match = re.fullmatch(rf"({digits})\s+In Stock", availability, flags=re.IGNORECASE)
        if not match:
            return None, "Mouser did not provide an unambiguous in-stock quantity"
        return int(match.group(1).replace(",", "")), "exact Mouser SKU match"
    except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError):
        return None, "Mouser lookup failed or returned an invalid response"


def _column(row: dict[str, str], *names: str) -> str:
    for name in names:
        if name in row:
            return (row[name] or "").strip()
    return ""


def _read_bom(path: Path) -> list[dict]:
    parts = []
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        if not reader.fieldnames:
            raise ValueError("BOM has no header")
        names = [name.strip().casefold() for name in reader.fieldnames]
        if len(set(names)) != len(names):
            raise ValueError("BOM contains duplicate columns")
        if not {"reference", "references", "ref", "refs"}.intersection(names):
            raise ValueError("BOM has no reference column")
        for index, raw in enumerate(reader, start=2):
            if None in raw:
                raise ValueError(f"BOM row {index} has too many fields")
            row = {key.strip().casefold(): value for key, value in raw.items()}
            if _column(row, "dnp", "do not populate").casefold() in {"yes", "true", "1", "dnp"} or _column(row, "in bom", "in_bom").casefold() in {"no", "false", "0"}:
                continue
            ref = _column(row, "reference", "references", "ref", "refs")
            if not ref:
                raise ValueError(f"BOM row {index} has a missing reference")
            qty = _column(row, "quantity", "qty")
            if not re.fullmatch(r"[1-9][0-9]*", qty):
                raise ValueError(f"BOM row {index} has a missing or invalid quantity")
            parts.append({"reference": ref, "quantity": int(qty), "digikey": _column(row, "digikey", "digikey sku", "digikey part number", "digikey_part_number"), "mouser": _column(row, "mouser", "mouser sku", "mouser part number", "mouser_part_number")})
    if not parts:
        raise ValueError("BOM has no populated parts")
    return parts


def check_inventory(bom: Path, build_quantity: int = 1, skip: bool = False) -> dict:
    """Check every populated BOM row; unknown and shortages fail closed.

    CSV columns are Reference, Qty, DigiKey and Mouser (SKU aliases accepted).
    The same SKU appearing on multiple rows is conservatively counted together.
    """
    if isinstance(build_quantity, bool) or not isinstance(build_quantity, int) or build_quantity < 1:
        return {"status": "unknown", "findings": [{"status": "unknown", "message": "Build quantity must be a positive integer"}]}
    if skip:
        return {"status": "skipped", "findings": [{"status": "skipped", "message": "Inventory verification skipped for an untrusted pull request"}]}
    try:
        parts = _read_bom(Path(bom))
    except (OSError, ValueError, csv.Error, UnicodeError, TypeError):
        return {"status": "unknown", "findings": [{"status": "unknown", "message": "BOM is missing, empty, or invalid; see the documented CSV format"}]}
    totals: dict[tuple[str, str], int] = defaultdict(int)
    for part in parts:
        for distributor in ("digikey", "mouser"):
            if part[distributor]:
                totals[(distributor, part[distributor].casefold())] += part["quantity"] * build_quantity
    cache = {}
    findings = []
    for part in parts:
        options = []
        for distributor, lookup in (("digikey", _digikey), ("mouser", _mouser)):
            sku = part[distributor]
            if not sku:
                continue
            key = (distributor, sku.casefold())
            if key not in cache:
                cache[key] = lookup(sku)
            available, detail = cache[key]
            required = totals[key]
            status = "unknown" if available is None else ("in_stock" if available >= required else "out_of_stock")
            options.append({"distributor": distributor, "sku": sku, "required": required, "available": available, "status": status, "detail": detail})
        status = "in_stock" if any(x["status"] == "in_stock" for x in options) else ("out_of_stock" if options and all(x["status"] == "out_of_stock" for x in options) else "unknown")
        findings.append({"reference": part["reference"], "quantity": part["quantity"] * build_quantity, "status": status, "options": options, "message": "No distributor SKU configured" if not options else ""})
    return {"status": "pass" if all(item["status"] == "in_stock" for item in findings) else "fail", "findings": findings}
