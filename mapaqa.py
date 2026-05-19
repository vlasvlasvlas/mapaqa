#!/usr/bin/env python3
"""mapaqa — monitor de puntos de control para plataformas mapainversiones."""

import html as _html
import json
import re
import ssl
import sys
import time
import urllib.request
import urllib.error
import yaml
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

TIMEOUT          = 15
CHECKPOINTS_FILE = Path(__file__).parent / "checkpoints.yaml"
REPORT_FILE      = Path(__file__).parent / "mapaqa_report.html"

# checks implied by checkpoint type
_TYPE_CHECKS: dict[str, list[str]] = {
    "home":            ["uptime", "response_time"],
    "map":             ["uptime", "response_time"],
    "project_profile": ["uptime"],
    "open_data":       ["uptime", "element", "freshness"],
    "generic":         ["uptime"],
}

GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"

_DEFAULT_SCRAPE = {
    "card_split": "<!-- Begin OpenData item -->",
    "name_regex": r'<h4 class="services-item-heading">([^<]+)</h4>',
    "date_regex": r"Última actualización:.*?<span[^>]+>(\d{2}-\d{2}-\d{4})</span>",
    "date_format": "%d-%m-%Y",
}


@dataclass
class DatasetInfo:
    name: str
    entity: str                                      # organismo fuente
    updated: str
    age_days: int
    stale: bool
    links: list[tuple[str, str]] = field(default_factory=list)  # (label, url)


@dataclass
class Result:
    id: str
    label: str
    country: str
    url: str
    ok: bool                                         # False solo si la URL no responde
    warn: bool = False                               # True si hay datasets desactualizados
    status_code: Optional[int] = None
    elapsed_ms: Optional[float] = None
    issues: list[str] = field(default_factory=list) # errores reales (HTTP, timeout)
    warnings: list[str] = field(default_factory=list) # avisos (freshness)
    datasets: list[DatasetInfo] = field(default_factory=list)


def fetch(url: str, timeout: int = TIMEOUT, ssl_verify: bool = True) -> tuple[int, float, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": "mapaqa-monitor/1.0"})
    ctx = None if ssl_verify else ssl.create_default_context()
    if ctx:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        body = resp.read()
        elapsed = (time.perf_counter() - t0) * 1000
        return resp.status, elapsed, body


_LINK_LABEL_RE = re.compile(r'>\s*([^<]{2,60}?)\s*<', re.DOTALL)


def _extract_links(card: str, link_re: re.Pattern, base: str) -> list[tuple[str, str]]:
    links = []
    for m in link_re.finditer(card):
        href = m.group(1)
        if not href.startswith("http"):
            href = base.rstrip("/") + href
        # label: primer texto visible después del href
        after = card[m.end():m.end() + 200]
        lm = _LINK_LABEL_RE.search(after)
        label = lm.group(1).strip() if lm else "Descargar"
        links.append((label, href))
    return links


def parse_datasets(html: str, scrape: dict, max_age_days: int) -> list[DatasetInfo]:
    today    = datetime.now(timezone.utc).date()
    card_sep = re.compile(scrape["card_split"])
    name_re  = re.compile(scrape["name_regex"])
    date_re  = re.compile(scrape["date_regex"], re.DOTALL)
    fmt      = scrape["date_format"]
    link_re  = re.compile(scrape["link_regex"]) if scrape.get("link_regex") else None
    base     = scrape.get("base_url", "")
    out = []
    for card in card_sep.split(html)[1:]:
        card = _html.unescape(card)
        nm = name_re.search(card)
        dm = date_re.search(card)
        if not (nm and dm):
            continue
        name, date_str = nm.group(1).strip(), dm.group(1)
        try:
            age   = (today - datetime.strptime(date_str, fmt).date()).days
            stale = age > max_age_days
        except ValueError:
            age, stale = -1, True
        links = _extract_links(card, link_re, base) if link_re else []
        if not links and scrape.get("fallback_link"):
            links = [("Ver datos", scrape["fallback_link"])]
        out.append(DatasetInfo(name=name, entity="", updated=date_str,
                               age_days=age, stale=stale, links=links))
    return out


def parse_datasets_api(base_url: str, html: str, scrape: dict, max_age_days: int,
                       ssl_verify: bool = True) -> list[DatasetInfo]:
    from urllib.parse import urljoin
    today   = datetime.now(timezone.utc).date()
    fmt     = scrape["date_format"]
    api_url = scrape["api_url"]
    if not api_url.startswith("http"):
        api_url = urljoin(base_url, api_url)

    _, _, body = fetch(api_url, ssl_verify=ssl_verify)
    data = json.loads(body)

    items: object = data
    for key in scrape.get("items_path", "").split("."):
        if key:
            items = items[key]

    id_field   = scrape.get("id_field", "idFuente")
    name_field = scrape["name_field"]
    date_field = scrape["date_field"]

    # index API data by fuente ID
    api_by_id: dict[int, dict] = {
        item[id_field]: {"entity": item.get(name_field, ""),
                         "date":   item.get(date_field, "")}
        for item in items if id_field in item
    }

    # hybrid: parse HTML for dataset names, fuente IDs, and download links
    if "card_split" in scrape and html:
        card_sep = re.compile(scrape["card_split"])
        name_re  = re.compile(scrape["name_regex"])
        fid_re   = re.compile(r'class="fuente(\d+)"')
        link_re  = re.compile(scrape["link_regex"]) if scrape.get("link_regex") else None
        base     = scrape.get("base_url", base_url.rstrip("/"))
        out = []
        for card in card_sep.split(html)[1:]:
            card = _html.unescape(card)
            nm  = name_re.search(card)
            fid = fid_re.search(card)
            if not (nm and fid):
                continue
            dataset_name = nm.group(1).strip()
            fuente_id    = int(fid.group(1))
            api_info     = api_by_id.get(fuente_id, {})
            entity       = api_info.get("entity", "")
            date_str     = api_info.get("date", "")
            links        = _extract_links(card, link_re, base) if link_re else []
            try:
                age   = (today - datetime.strptime(date_str, fmt).date()).days
                stale = age > max_age_days
            except ValueError:
                age, stale = -1, True
            out.append(DatasetInfo(name=dataset_name, entity=entity,
                                   updated=date_str, age_days=age,
                                   stale=stale, links=links))
        return out

    # fallback: API only, use entity name as dataset name
    out = []
    for item in items:
        entity   = item.get(name_field, "")
        date_str = item.get(date_field, "")
        try:
            age   = (today - datetime.strptime(date_str, fmt).date()).days
            stale = age > max_age_days
        except ValueError:
            age, stale = -1, True
        out.append(DatasetInfo(name=entity, entity=entity, updated=date_str,
                               age_days=age, stale=stale))
    return out


def check_uptime(cp: dict) -> Result:
    issues: list[str]          = []
    warnings: list[str]        = []
    datasets: list[DatasetInfo] = []
    ok = False
    warn = False
    status_code = elapsed_ms = None
    ssl_verify = cp.get("ssl_verify", True)

    try:
        status_code, elapsed_ms, body = fetch(cp["url"], ssl_verify=ssl_verify)
        if not ssl_verify:
            issues.append("SSL sin verificar")
        if status_code >= 400:
            issues.append(f"HTTP {status_code}")
        else:
            ok = True

        max_ms = cp.get("max_ms")
        if max_ms and elapsed_ms > max_ms:
            issues.append(f"lento: {elapsed_ms:.0f}ms > {max_ms}ms")
            ok = False

        html = body.decode("utf-8", errors="ignore")

        if "element" in cp.get("checks", []):
            text = cp.get("element_text", "")
            if text and text.lower() not in html.lower():
                issues.append(f"elemento no encontrado: '{text}'")
                ok = False

        if "freshness" in cp.get("checks", []):
            scrape = {**_DEFAULT_SCRAPE, **cp.get("scrape", {})}
            if scrape.get("mode") == "json_api":
                datasets = parse_datasets_api(cp["url"], html, scrape, cp.get("max_age_days", 30), ssl_verify=ssl_verify)
            else:
                datasets = parse_datasets(html, scrape, cp.get("max_age_days", 30))
            stale = [d for d in datasets if d.stale]
            if stale and datasets:
                pct = round(len(stale) / len(datasets) * 100)
                warnings.append(f"{len(stale)}/{len(datasets)} datasets desactualizados ({pct}%)")
                warn = True
                # ok no cambia: si la API/página respondió sigue siendo OK

    except urllib.error.HTTPError as e:
        status_code = e.code
        issues.append(f"HTTP {e.code}")
    except urllib.error.URLError as e:
        issues.append(f"conexión fallida: {e.reason}")
    except TimeoutError:
        issues.append("timeout")
    except Exception as e:
        issues.append(str(e))

    return Result(
        id=cp["id"], label=cp["label"], country=cp["country"],
        url=cp["url"], ok=ok, warn=warn, status_code=status_code,
        elapsed_ms=elapsed_ms, issues=issues, warnings=warnings, datasets=datasets,
    )


# ── terminal ─────────────────────────────────────────────────────────────────

def _osc8(url: str, text: str) -> str:
    return f"\033]8;;{url}\033\\{text}\033]8;;\033\\"


def _term_tag(ok: bool, warn: bool, issues: list[str]) -> str:
    if not ok:
        if any("lento" in i for i in issues):
            return f"{YELLOW}LENTO{RESET}"
        return f"{RED}FALLO{RESET}"
    if warn:
        return f"{YELLOW}WARN {RESET}"
    return f"{GREEN}OK   {RESET}"


def print_report(results: list[Result]) -> None:
    W = 84
    home_urls = {r.country: r.url for r in results if r.id.endswith("-home")}
    ts = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())

    print(f"\n{BOLD}mapaqa — estado de plataformas{RESET}  {DIM}{ts}{RESET}")
    print("═" * W)

    current_country = None
    for r in results:
        if r.country != current_country:
            current_country = r.country
            home = home_urls.get(r.country, "")
            home_part = f"  {DIM}{_osc8(home, home)}{RESET}" if home else ""
            print(f"\n  {BOLD}{CYAN}{r.country}{RESET}{home_part}")
            print(f"  {'─' * (W - 2)}")

        ms_str     = f"{r.elapsed_ms:.0f}ms" if r.elapsed_ms is not None else "    —"
        notes      = "; ".join(r.issues + r.warnings)
        notes_str  = f"  {DIM}{notes}{RESET}" if notes else ""
        tag        = _term_tag(r.ok, r.warn, r.issues)
        label      = r.label.split(" — ", 1)[-1] if " — " in r.label else r.label
        print(f"    {label:<44}  {tag}  {ms_str:>7}{notes_str}")

        for ds in r.datasets:
            ds_tag  = f"{RED}VIEJO{RESET}" if ds.stale else f"{GREEN}OK   {RESET}"
            age_str = f"{ds.age_days}d" if ds.age_days >= 0 else "?"
            print(f"      {DIM}│{RESET} {ds.name:<46}  {ds.updated}  {age_str:>5}  {ds_tag}")

    print()
    print("═" * W)
    total  = len(results)
    passed = sum(1 for r in results if r.ok)
    failed = total - passed
    c = GREEN if failed == 0 else (YELLOW if 0 < failed < total else RED)
    print(f"{c}{BOLD}{passed}/{total} OK{RESET}  {DIM}{failed} con problemas{RESET}\n")


# ── HTML ─────────────────────────────────────────────────────────────────────

def _flag(code: str) -> str:
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in code.upper())


def _badge(ok: bool, warn: bool, issues: list[str]) -> str:
    if not ok:
        if any("lento" in i for i in issues):
            return '<span class="badge b-warn">LENTO</span>'
        return '<span class="badge b-fail">FALLO</span>'
    if warn:
        return '<span class="badge b-warn">WARN</span>'
    return '<span class="badge b-ok">OK</span>'


def _age_cls(age_days: int, stale: bool) -> str:
    if stale or age_days < 0:
        return "a-fail"
    if age_days >= 14:
        return "a-warn"
    return "a-ok"


_CSS = """
:root{
  --ok:#16a34a;--ok-bg:#f0fdf4;--ok-br:#bbf7d0;
  --warn:#d97706;--warn-bg:#fffbeb;--warn-br:#fde68a;
  --fail:#dc2626;--fail-bg:#fef2f2;--fail-br:#fecaca;
  --border:#e5e7eb;--text:#111827;--muted:#6b7280;
  --bg:#f3f4f6;--card:#fff;
  --font:system-ui,-apple-system,"Segoe UI",sans-serif;
}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:var(--font);background:var(--bg);color:var(--text);
     padding:2rem;font-size:14px;line-height:1.5;max-width:1000px;margin:0 auto}
h1{font-size:1.375rem;font-weight:700}
.ts{color:var(--muted);font-size:.8rem;margin-top:.25rem}

/* global summary chips */
.summary{display:flex;gap:.75rem;margin:1.5rem 0;flex-wrap:wrap}
.sc{background:var(--card);border:1px solid var(--border);border-radius:8px;
    padding:.75rem 1.25rem;min-width:110px}
.sc .n{font-size:1.75rem;font-weight:700;line-height:1}
.sc .l{font-size:.7rem;color:var(--muted);text-transform:uppercase;
       letter-spacing:.06em;margin-top:.2rem}

/* country card */
.country{background:var(--card);border:1px solid var(--border);
         border-radius:10px;margin-bottom:1.25rem;overflow:hidden}

/* card header — always visible */
.ch{display:grid;grid-template-columns:auto 1fr auto;align-items:center;
    gap:1rem;padding:.9rem 1.25rem;background:#f8fafc;
    border-bottom:1px solid var(--border)}
.ch-id{display:flex;align-items:center;gap:.5rem}
.flag{font-size:1.5rem;line-height:1}
.cc{font-size:1rem;font-weight:700;letter-spacing:.04em}
.cl{color:var(--muted);font-size:.78rem;text-decoration:none;word-break:break-all}
.cl:hover{color:#2563eb;text-decoration:underline}
.ch-stats{display:flex;gap:.5rem;align-items:center;flex-wrap:wrap;justify-content:flex-end}

/* freshness summary line inside header */
.fresh{font-size:.8rem;color:var(--muted)}
.fresh strong{color:var(--text)}

/* collapsible sections */
details{border-top:1px solid var(--border)}
summary{
  display:flex;align-items:center;gap:.6rem;
  padding:.6rem 1.25rem;cursor:pointer;
  font-size:.75rem;font-weight:600;color:var(--muted);
  text-transform:uppercase;letter-spacing:.06em;
  user-select:none;list-style:none;background:#fafafa;
}
summary::-webkit-details-marker{display:none}
summary::before{content:"▶";font-size:.55rem;transition:transform .15s;color:var(--muted)}
details[open]>summary::before{transform:rotate(90deg)}
summary .cnt{background:#e5e7eb;border-radius:99px;padding:.05rem .45rem;
             font-size:.68rem;color:var(--text);font-weight:700}

/* tables */
table{width:100%;border-collapse:collapse}
th{text-align:left;padding:.4rem 1.25rem;font-size:.68rem;color:var(--muted);
   text-transform:uppercase;letter-spacing:.06em;
   border-bottom:1px solid var(--border);background:#fafafa;white-space:nowrap}
td{padding:.55rem 1.25rem;border-bottom:1px solid #f3f4f6;vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:#fafafa}
.cp-name{font-weight:500}

/* badges */
.badge{display:inline-block;padding:.15rem .55rem;border-radius:99px;
       font-size:.7rem;font-weight:700;letter-spacing:.02em;white-space:nowrap}
.b-ok  {background:var(--ok-bg);  color:var(--ok);  border:1px solid var(--ok-br)}
.b-warn{background:var(--warn-bg);color:var(--warn);border:1px solid var(--warn-br)}
.b-fail{background:var(--fail-bg);color:var(--fail);border:1px solid var(--fail-br)}

.ms{color:var(--muted);font-variant-numeric:tabular-nums;font-size:.8rem;white-space:nowrap}
.note{color:var(--fail);font-size:.78rem}
.warn-note{color:var(--warn);font-size:.78rem}
.cp-link{color:var(--text);text-decoration:none;font-weight:500}
.cp-link:hover{color:#2563eb;text-decoration:underline}
.recheck{color:var(--muted);text-decoration:none;font-size:.9rem;margin-left:.25rem;
         opacity:.5;transition:opacity .15s}
.recheck:hover{opacity:1;color:#2563eb}

/* dataset age coloring */
.a-ok  {color:var(--ok);  font-weight:600}
.a-warn{color:var(--warn);font-weight:600}
.a-fail{color:var(--fail);font-weight:700}

/* dataset name + entity */
.ds-name{display:block;font-weight:500;font-size:.82rem}
.ds-entity{display:block;font-size:.72rem;color:var(--muted);margin-top:.1rem}
.muted-sm{color:var(--muted);font-size:.75rem}

/* download links */
.dl-link{display:inline-flex;align-items:center;gap:.2rem;padding:.1rem .45rem;
         border-radius:4px;font-size:.7rem;font-weight:600;text-decoration:none;
         margin-right:.25rem;background:#eff6ff;color:#2563eb;border:1px solid #bfdbfe}
.dl-link::before{content:"🔗";font-size:.6rem}
.dl-link:hover{background:#dbeafe}

/* sortable headers */
th[data-col]{cursor:pointer;user-select:none;white-space:nowrap}
th[data-col]:hover{background:#f0f0f0}
.si{display:inline-block;margin-left:.3rem;font-size:.65rem;color:var(--muted);
    vertical-align:middle;font-weight:400}
"""

_JS = """
(function(){
  var DIAS_COL   = 2;
  var STATUS_COL = 4;

  function sortRows(table, col, dir) {
    var tbody = table.querySelector('tbody');
    if (!tbody) return;
    var rows = Array.from(tbody.querySelectorAll('tr'));
    var mul  = dir === 'asc' ? 1 : -1;
    rows.sort(function(a, b){
      var av = parseFloat(a.cells[col] && a.cells[col].dataset.sort) || 0;
      var bv = parseFloat(b.cells[col] && b.cells[col].dataset.sort) || 0;
      return (av - bv) * mul;
    });
    rows.forEach(function(r){ tbody.appendChild(r); });
  }

  function defaultSort(table) {
    var tbody = table.querySelector('tbody');
    if (!tbody) return;
    var rows = Array.from(tbody.querySelectorAll('tr'));
    rows.sort(function(a, b){
      var as = parseFloat(a.cells[STATUS_COL] && a.cells[STATUS_COL].dataset.sort) || 0;
      var bs = parseFloat(b.cells[STATUS_COL] && b.cells[STATUS_COL].dataset.sort) || 0;
      if (as !== bs) return as - bs;
      var ad = parseFloat(a.cells[DIAS_COL] && a.cells[DIAS_COL].dataset.sort) || 0;
      var bd = parseFloat(b.cells[DIAS_COL] && b.cells[DIAS_COL].dataset.sort) || 0;
      return ad - bd;
    });
    rows.forEach(function(r){ tbody.appendChild(r); });
  }

  document.querySelectorAll('th[data-col]').forEach(function(th){
    th.addEventListener('click', function(){
      var table = this.closest('table');
      var col   = parseInt(this.dataset.col);
      var prev  = this.dataset.dir;
      var dir   = prev === 'asc' ? 'desc' : 'asc';
      table.querySelectorAll('th[data-col]').forEach(function(h){
        h.dataset.dir = '';
        var si = h.querySelector('.si');
        if (si) si.textContent = '⇅';
      });
      this.dataset.dir = dir;
      var si = this.querySelector('.si');
      if (si) si.textContent = dir === 'asc' ? '↑' : '↓';
      sortRows(table, col, dir);
    });
  });

  document.querySelectorAll('details table').forEach(function(table){
    defaultSort(table);
    var th = table.querySelector('th[data-col="' + STATUS_COL + '"]');
    if (th) { th.dataset.dir = 'asc'; var si = th.querySelector('.si'); if(si) si.textContent='↑'; }
  });
})();
"""

_HTML = """\
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>mapaqa — estado</title>
<style>{css}</style>
</head>
<body>
<h1>mapaqa</h1>
<p class="ts">Estado de plataformas &nbsp;·&nbsp; {ts}</p>
{body}
<script>{js}</script>
</body>
</html>
"""


def generate_html(results: list[Result], ts: str) -> str:
    from collections import defaultdict

    # group by country preserving insertion order
    countries: list[str] = []
    by_country: dict[str, list[Result]] = defaultdict(list)
    for r in results:
        if r.country not in by_country:
            countries.append(r.country)
        by_country[r.country].append(r)

    home_urls = {r.country: r.url for r in results if r.id.endswith("-home")}

    # ── global summary (platform-level, not check-level) ─────────────────────
    platforms_ok    = sum(1 for c in countries if all(r.ok for r in by_country[c]))  # WARN = ok
    platforms_fail  = len(countries) - platforms_ok
    all_ds_global   = [ds for r in results for ds in r.datasets]
    datasets_stale  = sum(1 for ds in all_ds_global if ds.stale)
    datasets_ok_n   = sum(1 for ds in all_ds_global if not ds.stale)

    def chip(value: str, label: str, color: str) -> str:
        return (
            f'<div class="sc">'
            f'<div class="n" style="color:{color}">{value}</div>'
            f'<div class="l">{label}</div>'
            f'</div>'
        )

    chips = [
        chip(str(platforms_ok),   "Plataformas OK",          "var(--ok)"),
        chip(str(platforms_fail),  "Con problemas",           "var(--fail)" if platforms_fail else "var(--muted)"),
    ]
    if all_ds_global:
        chips.append(chip(str(datasets_ok_n),  "Actualizados",   "var(--ok)"))
        chips.append(chip(str(datasets_stale), "No actualizados", "var(--fail)" if datasets_stale else "var(--muted)"))
    summary_html = f'<div class="summary">{"".join(chips)}</div>'

    # ── per-country cards ─────────────────────────────────────────────────────
    sections: list[str] = []

    for country in countries:
        c_results  = by_country[country]
        checks_ok  = sum(1 for r in c_results if r.ok)   # WARN sigue siendo ok
        checks_tot = len(c_results)
        all_ds     = [ds for r in c_results for ds in r.datasets]
        stale_n    = sum(1 for ds in all_ds if ds.stale)
        valid_ds   = [ds for ds in all_ds if ds.age_days >= 0]

        # header
        home   = home_urls.get(country, "")
        home_a = f'<a href="{home}" class="cl" target="_blank">{home}</a>' if home else ""

        cc_cls       = "b-ok" if checks_ok == checks_tot else ("b-warn" if checks_ok else "b-fail")
        checks_badge = f'<span class="badge {cc_cls}">{checks_ok}/{checks_tot} checks</span>'

        fresh_html = ""
        if valid_ds:
            newest    = min(valid_ds, key=lambda d: d.age_days)
            ac        = _age_cls(newest.age_days, newest.stale)
            fresh_html = (
                f'<span class="fresh">Últ. actualización: '
                f'<strong>{newest.updated}</strong> &nbsp;·&nbsp; '
                f'<span class="{ac}"><strong>{newest.age_days}</strong> días</span></span>'
            )
        if stale_n:
            fresh_html += (
                f' <span class="badge b-fail">'
                f'⚠ {stale_n} dataset{"s" if stale_n > 1 else ""} desactualizado{"s" if stale_n > 1 else ""}'
                f'</span>'
            )

        header = (
            f'<div class="ch">'
            f'<div class="ch-id"><span class="flag">{_flag(country)}</span>'
            f'<span class="cc">{country}</span></div>'
            f'<div>{home_a}</div>'
            f'<div class="ch-stats">{checks_badge}&nbsp;{fresh_html}</div>'
            f'</div>'
        )

        # checks collapsible — open if any failed
        check_rows = []
        for r in c_results:
            ms_str   = f"{r.elapsed_ms:.0f}&nbsp;ms" if r.elapsed_ms is not None else "—"
            all_notes = r.issues + r.warnings
            note_cls  = "note" if r.issues else "warn-note"
            note      = f'<span class="{note_cls}">{"; ".join(all_notes)}</span>' if all_notes else ""
            label     = r.label.split(" — ", 1)[-1] if " — " in r.label else r.label
            cp_link   = f'<a href="{r.url}" target="_blank" class="cp-link">{label}</a>'
            recheck   = f'<a href="{r.url}" target="_blank" class="recheck" title="Abrir y verificar manualmente">⟳</a>'
            check_rows.append(
                f'<tr><td>{cp_link}&nbsp;{recheck}</td><td>{_badge(r.ok, r.warn, r.issues)}</td>'
                f'<td class="ms">{ms_str}</td><td>{note}</td></tr>'
            )
        checks_open  = "" if checks_ok == checks_tot else " open"
        checks_block = (
            f'<details{checks_open}>'
            f'<summary>Checks <span class="cnt">{checks_tot}</span></summary>'
            f'<table><thead><tr><th>URL / Sección</th><th>Estado</th>'
            f'<th>Tiempo</th><th>Notas</th></tr></thead>'
            f'<tbody>{"".join(check_rows)}</tbody></table>'
            f'</details>'
        )

        # datasets collapsible — open if any stale
        ds_block = ""
        if all_ds:
            ds_rows = []
            for ds in all_ds:
                age_str  = f"{ds.age_days}" if ds.age_days >= 0 else "?"
                ac       = _age_cls(ds.age_days, ds.stale)
                sort_status  = "2" if ds.stale else "0"
                ds_badge     = (
                    '<span class="badge b-fail">DESACTUALIZADO</span>'
                    if ds.stale else
                    '<span class="badge b-ok">OK</span>'
                )
                links_html   = " ".join(
                    f'<a href="{url}" target="_blank" class="dl-link">{label}</a>'
                    for label, url in ds.links
                ) if ds.links else '<span class="muted-sm">—</span>'
                entity_html  = f'<span class="ds-entity">{ds.entity}</span>' if ds.entity else ""
                safe_age     = ds.age_days if ds.age_days >= 0 else 99999
                ds_rows.append(
                    f'<tr>'
                    f'<td><span class="ds-name">{ds.name}</span>{entity_html}</td>'
                    f'<td class="ms">{ds.updated}</td>'
                    f'<td class="{ac}" data-sort="{safe_age}"><strong>{age_str}</strong> días</td>'
                    f'<td>{links_html}</td>'
                    f'<td data-sort="{sort_status}">{ds_badge}</td>'
                    f'</tr>'
                )
            stale_badge = (
                f' <span class="badge b-fail">'
                f'{stale_n} desactualizado{"s" if stale_n > 1 else ""}'
                f'</span>'
            ) if stale_n else ""
            ds_open  = " open" if stale_n else ""
            ds_block = (
                f'<details{ds_open}>'
                f'<summary>Open Data <span class="cnt">{len(all_ds)}</span>{stale_badge}</summary>'
                f'<table><thead><tr>'
                f'<th>Dataset / Entidad</th>'
                f'<th>Últ. actualización</th>'
                f'<th data-col="2">Días<span class="si">⇅</span></th>'
                f'<th>Recursos</th>'
                f'<th data-col="4">Estado<span class="si">⇅</span></th>'
                f'</tr></thead>'
                f'<tbody>{"".join(ds_rows)}</tbody></table>'
                f'</details>'
            )

        sections.append(f'<div class="country">{header}{checks_block}{ds_block}</div>')

    return _HTML.format(
        css=_CSS, js=_JS, ts=ts,
        body=summary_html + "\n".join(sections),
    )


# ── main ─────────────────────────────────────────────────────────────────────

def load_checkpoints(path: Path) -> list[dict]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    defaults     = raw.get("defaults", {})
    type_defs    = raw.get("type_defaults", {})
    checkpoints  = raw.get("checkpoints", [])

    resolved = []
    for cp in checkpoints:
        cp_type = cp.get("type", "generic")
        td      = type_defs.get(cp_type, {})
        r       = {**defaults, **td, **cp}

        # derive checks from type if not explicit
        if "checks" not in r:
            checks = list(_TYPE_CHECKS.get(cp_type, ["uptime"]))
            # drop element if no element_text
            if "element" in checks and not r.get("element_text"):
                checks.remove("element")
            # drop freshness if no scrape block
            if "freshness" in checks and not r.get("scrape"):
                checks.remove("freshness")
            r["checks"] = checks

        # map response_time_ms → max_ms used internally
        if "response_time_ms" in r and "max_ms" not in r:
            r["max_ms"] = r["response_time_ms"]

        resolved.append(r)
    return resolved


def main(checkpoints_path: Path = CHECKPOINTS_FILE) -> int:
    if not checkpoints_path.exists():
        print(f"ERROR: no se encontró {checkpoints_path}", file=sys.stderr)
        return 1

    checkpoints = load_checkpoints(checkpoints_path)
    print(f"Verificando {len(checkpoints)} puntos de control...", end="", flush=True)

    results = []
    for cp in checkpoints:
        result = check_uptime(cp)
        print("." if result.ok else "!", end="", flush=True)
        results.append(result)

    print()
    ts = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    print_report(results)

    html = generate_html(results, ts)
    REPORT_FILE.write_text(html, encoding="utf-8")
    print(f"{DIM}Reporte HTML → {REPORT_FILE}{RESET}\n")

    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
