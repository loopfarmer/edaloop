"""atopile's parts API, served locally from the pcbparts catalogue.

atopile's hosted service, components.atopileapi.com, no longer resolves, which
stops parametric picking. atopile lets the URL be overridden, so this speaks its
wire protocol and answers from pcbparts' SQLite database. The database is opened
read-only.

Selection rule: a part qualifies only if its whole tolerance band lies inside
the band the design asks for, since atopile rejects anything else. Among those,
basic and preferred parts come first, since JLCPCB adds no assembly fee for them.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import toolchain

# Tolerance assumed where the catalogue states none, so a part never reads tighter than it may be.
DEFAULT_TOLERANCE_PCT = {"resistors": 5.0, "capacitors": 20.0, "inductors": 30.0}
MAX_RESULTS = 50


def _interval(data):
    """(min, max) from atopile's Quantity_Interval_Disjoint, or None when unconstrained."""
    if not data or data.get("type") != "Quantity_Interval_Disjoint":
        return None
    spans = data["data"]["intervals"]["data"]["intervals"]
    if not spans:
        return None
    lows = [s["data"]["min"] for s in spans]
    highs = [s["data"]["max"] for s in spans]
    return (None if None in lows else min(lows), None if None in highs else max(highs))


def _quantity(low: float, high: float, unit: str) -> dict:
    return {"type": "Quantity_Interval_Disjoint", "data": {
        "intervals": {"type": "Numeric_Interval_Disjoint", "data": {
            "intervals": [{"type": "Numeric_Interval", "data": {"min": low, "max": high}}]}},
        "unit": unit}}


def _point(value, unit):
    return None if value is None else _quantity(float(value), float(value), unit)


def _package_sizes(package) -> list[str] | None:
    """atopile sends R0402, C0402 and the like. The catalogue stores 0402."""
    if not package:
        return None
    values = package.get("data", {}).get("values") or package.get("data", {}).get("elements") or []
    sizes = []
    for v in values:
        text = v.get("name", "") if isinstance(v, dict) else str(v)
        m = re.search(r"(\d{4,5})", text)
        if m:
            sizes.append(m.group(1))
    return sizes or None


# endpoint -> (value column, unit, catalogue category, atopile parameter)
ENDPOINTS = {
    "resistors": ("resistance_ohms", "ohm", "Resistors", "resistance"),
    "capacitors": ("capacitance_f", "farad", "Capacitors", "capacitance"),
    "inductors": ("inductance_h", "henry", "Inductors, Coils, Chokes", "inductance"),
}

# atopile parameter -> (column, unit). The design states the lowest acceptable rating.
RATINGS = {
    "max_power": ("power_w", "watt"),
    "max_voltage": ("voltage_max_v", "volt"),
    "max_current": ("current_max_a", "ampere"),
    "saturation_current": ("isat_a", "ampere"),
}


class Catalogue:
    def __init__(self, db_path: Path | str | None = None):
        self.path = str(db_path or toolchain.paths()["pcbparts_db"])

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        return c

    def _component(self, row, endpoint: str | None = None) -> dict:
        number = int(row["lcsc"].lstrip("Cc"))
        attributes: dict = {}
        if endpoint:
            column, unit, _, param = ENDPOINTS[endpoint]
            tol = row["tolerance_pct"] if row["tolerance_pct"] else DEFAULT_TOLERANCE_PCT[endpoint]
            value = row[column]
            attributes[param] = _quantity(value * (1 - tol / 100), value * (1 + tol / 100), unit)
            for name, (col, u) in RATINGS.items():
                if col in row.keys() and _applies(endpoint, name):
                    attributes[name] = _point(row[col], u)
            if endpoint == "inductors":
                attributes["dc_resistance"] = _point(row["dcr_ohms"], "ohm")
        return {"lcsc": number, "manufacturer_name": row["manufacturer"] or "", "part_number": row["mpn"] or "",
                "package": row["package"] or "",
                "datasheet_url": f"https://www.lcsc.com/product-detail/C{number}.html",
                "description": row["description"] or "",
                "is_basic": int(row["library_type"] == "b"), "is_preferred": int(row["library_type"] == "p"),
                "stock": row["stock"] or 0,
                "price": [{"qFrom": 1, "qTo": None, "price": row["price"] or 0.0}], "attributes": attributes}

    def by_lcsc(self, number: int) -> list[dict]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM components WHERE lcsc = ?", (f"C{number}",)).fetchone()
        return [self._component(row)] if row else []

    def by_mfr(self, manufacturer: str, part_number: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM components WHERE mpn = ? ORDER BY stock DESC LIMIT ?",
                             (part_number, MAX_RESULTS)).fetchall()
        wanted = manufacturer.lower()
        hits = [r for r in rows if wanted in (r["manufacturer"] or "").lower()] or rows
        return [self._component(r) for r in hits]

    def query(self, endpoint: str, params: dict) -> list[dict]:
        column, _, category, param = ENDPOINTS[endpoint]
        band = _interval(params.get(param))
        if band is None or band[0] is None or band[1] is None:
            return []
        low, high = band
        qty = int(params.get("qty") or 1)
        default_tol = DEFAULT_TOLERANCE_PCT[endpoint]
        # The tolerance band must fit inside the requested band, with room for float rounding.
        sql = [f"SELECT c.* FROM components c JOIN subcategories s ON s.id = c.subcategory_id",
               f"WHERE s.category_name = ? AND c.{column} BETWEEN ? AND ? AND c.stock >= ?",
               f"AND c.{column} * (1 - COALESCE(c.tolerance_pct, ?) / 100.0) >= ? * 0.999999",
               f"AND c.{column} * (1 + COALESCE(c.tolerance_pct, ?) / 100.0) <= ? * 1.000001"]
        args: list = [category, low, high, qty, default_tol, low, default_tol, high]
        if endpoint == "inductors":
            sql.append("AND s.name LIKE '%Inductor%'")   # the category also holds transformers
        sizes = _package_sizes(params.get("package"))
        if sizes:
            sql.append(f"AND c.package IN ({','.join('?' * len(sizes))})")
            args += sizes
        for name, (col, _) in RATINGS.items():
            rating = _interval(params.get(name))
            if rating and rating[0] is not None and _applies(endpoint, name):
                sql.append(f"AND c.{col} >= ?")
                args.append(rating[0])
        sql.append("ORDER BY CASE c.library_type WHEN 'b' THEN 0 WHEN 'p' THEN 1 ELSE 2 END, c.price, c.stock DESC LIMIT ?")
        args.append(MAX_RESULTS)
        with self._conn() as c:
            rows = c.execute(" ".join(sql), args).fetchall()
        return [self._component(r, endpoint) for r in rows]

    def batch(self, query: dict) -> list[dict]:
        if "lcsc" in query:
            return self.by_lcsc(int(query["lcsc"]))
        if "part_number" in query:
            return self.by_mfr(query.get("manufacturer_name", ""), query["part_number"])
        return self.query(query["endpoint"], query)


def _applies(endpoint: str, rating: str) -> bool:
    return {"resistors": {"max_power", "max_voltage"}, "capacitors": {"max_voltage"},
            "inductors": {"max_current", "saturation_current"}}[endpoint].__contains__(rating)


def make_server(port: int = 0, db_path: Path | str | None = None) -> ThreadingHTTPServer:
    catalogue = Catalogue(db_path)

    class Handler(BaseHTTPRequestHandler):
        def _send(self, payload, status=200):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parts = [p for p in self.path.split("/") if p]
            if parts[:3] == ["v0", "component", "lcsc"] and len(parts) == 4:
                return self._send({"components": catalogue.by_lcsc(int(parts[3].lstrip("Cc")))})
            if parts[:3] == ["v0", "component", "mfr"] and len(parts) == 5:
                return self._send({"components": catalogue.by_mfr(parts[3], parts[4])})
            self._send({"detail": "unknown route"}, 404)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            if self.path == "/v0/query":
                return self._send({"results": [{"components": catalogue.batch(q)} for q in body["queries"]]})
            parts = [p for p in self.path.split("/") if p]
            if parts[:2] == ["v0", "query"] and len(parts) == 3 and parts[2] in ENDPOINTS:
                return self._send({"components": catalogue.query(parts[2], body)})
            self._send({"detail": "unknown route"}, 404)

        def log_message(self, *args):
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


class Running:
    """The server on a free local port for the length of a `with` block."""

    def __init__(self, db_path=None):
        self.server = make_server(0, db_path)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
