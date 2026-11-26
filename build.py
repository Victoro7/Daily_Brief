#!/usr/bin/env python3
"""Diagnostics Desk: collect -> tag -> daily brief -> render static pages.

Run locally:   python build.py
Runs daily on GitHub Actions (see .github/workflows/update.yml).
Outputs: index.html (feed), brief.html (latest brief), briefs/YYYY-MM-DD.(md|html)
"""
import datetime as dt
import hashlib
import html
import json
import os
import re
from pathlib import Path
from urllib.parse import quote_plus

import feedparser
import markdown
import requests

ROOT = Path(__file__).parent
STORE = ROOT / "data" / "items.json"
BRIEFS = ROOT / "briefs"
KEEP_DAYS = 60                 # how long items stay in the feed
BRIEF_HOURS = 48               # window of items sent to the brief
MAX_BRIEF_ITEMS = 50           # cap on items sent to the model
MODEL = os.getenv("BRIEF_MODEL", "claude-sonnet-5-5")
UA = {"User-Agent": "dx-desk/1.0 (personal research tool)"}
NOW = dt.datetime.now(dt.timezone.utc)

# ---------------------------------------------------------------- SOURCES --
# Edit these lists to tune your niche. Broken feeds are skipped, not fatal.

GOOGLE_QUERIES = [
    '"liquid biopsy" OR "multi-cancer early detection" OR MCED',
    '"breast cancer" (diagnostic OR screening OR "blood test" OR mammography)',
    '"cancer diagnostics" (raises OR funding OR "Series A" OR "Series B" OR acquisition)',
    '"companion diagnostic" OR "in vitro diagnostic" OR "laboratory developed test"',
    'diagnostic ("510(k)" OR PMA OR "breakthrough device" OR "de novo") cancer',
    'AI (pathology OR mammography OR radiology) FDA clearance cancer',
    '(CMS OR Medicare OR MolDX OR USPSTF) (diagnostic OR screening OR "early detection")',
    '"circulating tumor DNA" OR ctDNA OR "minimal residual disease"',
]

# General feeds are filtered for diagnostics relevance (INCLUDE below).
TRADE_FEEDS = {
    "Fierce Biotech": "https://www.fiercebiotech.com/rss/xml",
    "Fierce Healthcare": "https://www.fiercehealthcare.com/rss/xml",
    "STAT": "https://www.statnews.com/feed/",
    "MedCity News": "https://medcitynews.com/feed/",
    "GenomeWeb": "https://www.genomeweb.com/rss.xml",
    "FDA Press": "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml",
}

PUBMED_TERM = (
    '(("breast neoplasms"[MeSH] OR "breast cancer"[tiab]) AND '
    '(diagnosis[tiab] OR diagnostic[tiab] OR screening[tiab] OR "liquid biopsy"[tiab] '
    'OR ctDNA[tiab] OR mammography[tiab])) OR "multi-cancer early detection"[tiab]'
)
TRIALS_QUERY = {
    "query.cond": "breast cancer",
    "query.term": 'diagnostic OR screening OR "liquid biopsy" OR ctDNA',
    "sort": "LastUpdatePostDate:desc",
    "pageSize": 15,
    "format": "json",
}

# ----------------------------------------------------------------- RULES ---
INCLUDE = re.compile(
    r"diagnos|screening|biops|ctdna|early detection|mammogra|patholog|assay|biomarker|"
    r"companion|in vitro|liquid|sequencing|genomic test|molecular test|\bldt\b|\bivd\b",
    re.I,
)
EXCLUDE = re.compile(
    r"class action|lead plaintiff|investor alert|shareholder alert|price target|stock forecast|"
    r"horoscope|energy drink|veterinar|\bpet\b|cosmetic|dental|skincare|crypto|casino",
    re.I,
)
# First match wins, so order matters.
CATEGORY_RULES = [
    ("Funding & Deals", re.compile(
        r"\braises?\b|\braised\b|funding|series [a-d]\b|seed round|financing|acquir|acquisition|"
        r"merger|\bipo\b|\bspac\b|venture|partnership|licens|\bbuys?\b|\bsells?\b", re.I)),
    ("Regulatory", re.compile(
        r"\bfda\b|510\(k\)|\bpma\b|clearance|cleared|approval|approved|breakthrough device|"
        r"de novo|ce mark|\bldt\b|authoriz|\bemergency use\b", re.I)),
    ("Reimbursement & Policy", re.compile(
        r"\bcms\b|medicare|medicaid|reimburs|coverage|moldx|\bcpt\b|uspstf|legislation|\bbill\b|"
        r"\bact\b|congress|payer|insurer|\bpolicy\b", re.I)),
    ("Science & Trials", re.compile(
        r"trial|study|studies|results|publish|journal|nejm|lancet|sensitivity|specificity|"
        r"validation|cohort|ctdna|biomarker|data show", re.I)),
]
CATEGORIES = [c for c, _ in CATEGORY_RULES]
CAT_WEIGHT = {"Funding & Deals": 3, "Regulatory": 3, "Reimbursement & Policy": 2,
              "Science & Trials": 2}

# --------------------------------------------------------------- HELPERS ---


def clean(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def norm(title):
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def categorize(text):
    for name, rx in CATEGORY_RULES:
        if rx.search(text):
            return name
    return "Industry"


def make_item(title, link, source, when, summary="", force_cat=None):
    title, summary = clean(title), clean(summary)
    if not title or not link or EXCLUDE.search(title):
        return None
    if summary.lower().startswith(title.lower()[:40]):
        summary = ""                       # Google News echoes the title
    return {
        "id": hashlib.md5(norm(title).encode()).hexdigest()[:12],
        "title": title,
        "link": link,
        "source": source,
        "date": when.isoformat(),
        "summary": summary[:300],
        "category": force_cat or categorize(f"{title} {summary}"),
    }


def entry_date(entry):
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return dt.datetime(*t[:6], tzinfo=dt.timezone.utc)
    return NOW


def fetch_rss(url, label=None, relevance=False):
    items = []
    try:
        r = requests.get(url, headers=UA, timeout=30)
        r.raise_for_status()
        feed = feedparser.parse(r.content)
    except Exception as exc:                # noqa: BLE001
        print(f"[warn] {label or url[:60]}: {exc}")
        return items
    for e in feed.entries:
        title = e.get("title", "")
        summary = e.get("summary", "")
        source = label
        if not source:                      # Google News: "Headline - Outlet"
            source = (e.get("source") or {}).get("title")
            if " - " in title:
                head, tail = title.rsplit(" - ", 1)
                title, source = head, source or tail
            source = source or "Google News"
        if relevance and not INCLUDE.search(f"{title} {clean(summary)}"):
            continue
        item = make_item(title, e.get("link"), source, entry_date(e), summary)
        if item:
            items.append(item)
    print(f"[ok] {label or url[:60]}: {len(items)} items")
    return items


def fetch_google(days):
    items = []
    for q in GOOGLE_QUERIES:
        url = ("https://news.google.com/rss/search?q="
               f"{quote_plus(q + f' when:{days}d')}&hl=en-US&gl=US&ceid=US:en")
        items += fetch_rss(url)
    return items


def fetch_pubmed():
    items = []
    try:
        base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
        ids = requests.get(base + "esearch.fcgi", headers=UA, timeout=30, params={
            "db": "pubmed", "term": PUBMED_TERM, "reldate": 7, "datetype": "edat",
            "retmax": 25, "sort": "date", "retmode": "json"}).json()["esearchresult"]["idlist"]
        if not ids:
            return items
        res = requests.get(base + "esummary.fcgi", headers=UA, timeout=30, params={
            "db": "pubmed", "id": ",".join(ids), "retmode": "json"}).json()["result"]
        for uid in ids:
            d = res[uid]
            when = dt.datetime.strptime(d.get("sortpubdate", "")[:10], "%Y/%m/%d").replace(
                tzinfo=dt.timezone.utc) if d.get("sortpubdate") else NOW
            item = make_item(d["title"], f"https://pubmed.ncbi.nlm.nih.gov/{uid}/", "PubMed",
                             when, d.get("fulljournalname", ""), "Science & Trials")
            if item:
                items.append(item)
    except Exception as exc:                # noqa: BLE001
        print(f"[warn] PubMed: {exc}")
    print(f"[ok] PubMed: {len(items)} items")
    return items


def fetch_trials():
    items = []
    try:
        data = requests.get("https://clinicaltrials.gov/api/v2/studies", headers=UA,
                            timeout=30, params=TRIALS_QUERY).json()
        for s in data.get("studies", []):
            p = s["protocolSection"]
            nct = p["identificationModule"]["nctId"]
            title = p["identificationModule"].get("briefTitle", "")
            updated = p["statusModule"]["lastUpdatePostDateStruct"]["date"]
            when = dt.datetime.strptime(updated[:10], "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
            item = make_item(f"Trial update: {title}", f"https://clinicaltrials.gov/study/{nct}",
                             "ClinicalTrials.gov", when, "", "Science & Trials")
            if item:
                items.append(item)
    except Exception as exc:                # noqa: BLE001
        print(f"[warn] ClinicalTrials.gov: {exc}")
    print(f"[ok] ClinicalTrials.gov: {len(items)} items")
    return items


# ----------------------------------------------------------------- STORE ---


def load_store():
    if STORE.exists():
        return {i["id"]: i for i in json.loads(STORE.read_text())}
    return {}


def save_store(store):
    cutoff = NOW - dt.timedelta(days=KEEP_DAYS)
    keep = [i for i in store.values() if dt.datetime.fromisoformat(i["date"]) >= cutoff]
    keep.sort(key=lambda i: i["date"], reverse=True)
    STORE.parent.mkdir(exist_ok=True)
    STORE.write_text(json.dumps(keep, indent=1))
    return keep


# ----------------------------------------------------------------- BRIEF ---


def brief_candidates(items):
    cutoff = NOW - dt.timedelta(hours=BRIEF_HOURS)
    recent = [i for i in items if dt.datetime.fromisoformat(i["date"]) >= cutoff]
    if len(recent) < 15:                    # thin day: fall back to newest items
        recent = items[:40]

    def score(i):
        s = CAT_WEIGHT.get(i["category"], 1)
        s += 2 if re.search(r"breast", i["title"], re.I) else 0
        s += 1 if i["source"] in ("STAT", "Fierce Biotech", "GenomeWeb", "FDA Press") else 0
        return s
    return sorted(recent, key=score, reverse=True)[:MAX_BRIEF_ITEMS]


def make_brief(items, today):
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key or not items:
        print("[skip] brief: no API key or no items")
        return None
    import anthropic
    lines = []
    for n, i in enumerate(items, 1):
        extra = f" | snippet: {i['summary']}" if i["summary"] else ""
        lines.append(f"[{n}] ({i['category']} | {i['source']} | {i['date'][:10]}) "
                     f"{i['title']} | {i['link']}{extra}")
    system = (ROOT / "brief_prompt.txt").read_text().replace("{date}", today)
    client = anthropic.Anthropic(api_key=key)
    msg = client.messages.create(
        model=MODEL, max_tokens=3000, system=system,
        messages=[{"role": "user",
                   "content": f"Today is {today}. Items:\n\n" + "\n".join(lines)}])
    return "".join(b.text for b in msg.content if b.type == "text").strip()


# ---------------------------------------------------------------- RENDER ---

CSS = """
:root{--bg:#f1f4f3;--panel:#fff;--ink:#14222b;--mute:#5b6b73;--line:#cfd9d6;--link:#0b5f63;
--c-funding:#0f6b6b;--c-reg:#2a4b8d;--c-pol:#a8660c;--c-sci:#b0245a;--c-ind:#5b6b73}
@media (prefers-color-scheme:dark){:root{--bg:#10191d;--panel:#16222733;--ink:#e6eeee;--mute:#93a5ab;
--line:#2a3a40;--link:#6fd0d0;--c-funding:#58c4c4;--c-reg:#8fb0f0;--c-pol:#e0a24d;--c-sci:#f07fa8;--c-ind:#93a5ab}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 "IBM Plex Sans",system-ui,sans-serif}
a{color:var(--link)}a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid var(--link);outline-offset:2px}
.wrap{max-width:860px;margin:0 auto;padding:28px 18px 80px}
h1,h2,h3{font-family:Newsreader,Georgia,serif;font-weight:600;line-height:1.2}
header h1{font-size:clamp(2rem,6vw,2.9rem);margin:0 0 4px;letter-spacing:-.01em}
header p{margin:0;color:var(--mute)}
.top{display:flex;flex-wrap:wrap;gap:10px 18px;align-items:baseline;margin-top:12px}
.brieflink{font-weight:600}
.bar{position:sticky;top:0;z-index:5;background:var(--bg);padding:14px 0 12px;border-bottom:1px solid var(--line);margin-top:20px}
.row{display:flex;flex-wrap:wrap;gap:8px}
.bar input,.bar select{font:inherit;padding:8px 10px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--ink)}
.bar input{flex:1 1 200px;min-width:0}
.chip{font:inherit;font-size:.9rem;padding:5px 12px;border:1px solid var(--line);border-radius:999px;background:transparent;color:var(--ink);cursor:pointer}
.chip[aria-pressed=true]{background:var(--ink);color:var(--bg);border-color:var(--ink)}
#count{color:var(--mute);font-size:.9rem;margin:10px 0 0}
.day h2{font-size:1.15rem;margin:30px 0 6px;color:var(--mute)}
ul{list-style:none;margin:0;padding:0}
.item{padding:10px 0 10px 14px;border-left:3px solid var(--cc,var(--c-ind));margin:0 0 6px}
.item[hidden],.day[hidden]{display:none}
.item a.t{font-family:Newsreader,Georgia,serif;font-size:1.12rem;font-weight:500;line-height:1.3;text-decoration:none;color:var(--ink)}
.item a.t:hover{text-decoration:underline}
.meta{font-size:.84rem;color:var(--mute);display:flex;flex-wrap:wrap;gap:2px 14px;margin-top:2px}
.meta .cat{color:var(--cc);font-weight:600}
.snip{font-size:.9rem;color:var(--mute);margin:3px 0 0}
.brief{font-family:Newsreader,Georgia,serif;font-size:1.12rem;line-height:1.65}
.brief h1{font-size:2rem}.brief h2{font-size:1.4rem;margin:2em 0 .4em}
.brief p,.brief li{max-width:68ch}.brief ul{list-style:disc;padding-left:1.3em}
.past{margin-top:48px;padding-top:16px;border-top:1px solid var(--line);font-family:"IBM Plex Sans",sans-serif;font-size:.95rem}
.past a{margin-right:14px;white-space:nowrap}
"""

CAT_VAR = {"Funding & Deals": "--c-funding", "Regulatory": "--c-reg",
           "Reimbursement & Policy": "--c-pol", "Science & Trials": "--c-sci",
           "Industry": "--c-ind"}

FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
         '<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;600'
         '&family=Newsreader:opsz,wght@6..72,500;6..72,600&display=swap" rel="stylesheet">')

INDEX_CSS = """
:root{--bg:#f5f0e6;--card:#fffdf8;--text:#141414;--muted:#6e675c;--line:#e2dacb;
--accent:#b3261e;--accent-soft:#f6e1dc;--c-funding:#0f6b6b;--c-reg:#2a4b8d;--c-pol:#a8660c;
--c-sci:#b0245a;--c-ind:#6e675c}
@media (prefers-color-scheme:dark){:root{--bg:#111111;--card:#1b1b1a;--text:#f5f0e6;--muted:#a39d92;
--line:#2c2b29;--accent:#ef5a4c;--accent-soft:#3b1714;--c-funding:#58c4c4;--c-reg:#8fb0f0;
--c-pol:#e0a24d;--c-sci:#f07fa8;--c-ind:#a39d92}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;-webkit-text-size-adjust:100%}
a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
header{position:sticky;top:0;z-index:2;background:var(--bg);padding:14px 16px 12px;border-bottom:1px solid var(--line)}
h1{margin:0;font:700 23px/1.15 Georgia,"Times New Roman",serif;letter-spacing:-.01em}
.subrow{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:4px 0 12px}
.updated,#count{margin:0;color:var(--muted);font-size:13px}
.jump-select{-webkit-appearance:none;appearance:none;flex:none;min-width:0;max-width:52%;border:1px solid var(--line);border-radius:999px;background-color:var(--card);color:var(--text);font-family:inherit;font-size:13px;font-weight:600;padding:6px 28px 6px 12px;cursor:pointer;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M1 1l4 4 4-4' fill='none' stroke='%238a8378' stroke-width='1.6'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 11px center}
.filters{display:flex;gap:8px;overflow-x:auto;margin:0 -16px;padding:0 16px;scrollbar-width:none}
.filters::-webkit-scrollbar{display:none}
.filters button{flex:none;border:1px solid var(--line);border-radius:999px;padding:6px 12px;font-family:inherit;font-size:14px;font-weight:600;background:var(--card);color:var(--muted);cursor:pointer}
.filters button[aria-pressed="true"]{background:var(--accent-soft);border-color:transparent;color:var(--accent)}
.filters .n{display:none}
.controls{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
.controls input,.controls select{font-family:inherit;font-size:14px;padding:7px 10px;border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--text);min-width:0}
.controls input{flex:1 1 100%;min-width:0}
.controls select{flex:1 1 0;max-width:100%}
.brief-row{margin:10px 0 0}
.brieflink{font-weight:600;color:var(--accent)}
main{max-width:680px;margin:0 auto;padding:4px 16px 32px}
.month{scroll-margin-top:140px}
.month[hidden],li[hidden]{display:none}
h2{font-size:14px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:22px 0 8px}
h2 span{display:none}
ul{list-style:none;margin:0;padding:0;background:var(--card);border:1px solid var(--line);border-radius:14px;overflow:hidden}
li+li{border-top:1px solid var(--line)}
li.first-visible{border-top:0}
li a{display:block;padding:12px 14px;color:inherit;text-decoration:none}
li a:active{background:var(--accent-soft)}
.tag{display:inline-block;font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--cc,var(--c-ind));margin-bottom:3px}
.headline{font-weight:600}
.meta{margin-top:3px;font-size:13px;color:var(--muted)}
.more{display:block;width:100%;margin-top:8px;padding:11px;border-radius:12px;border:1px solid var(--line);background:transparent;color:var(--accent);font-family:inherit;font-size:14px;font-weight:600;cursor:pointer}
.more[hidden]{display:none}
.empty{margin-top:24px;padding:14px;color:var(--muted);text-align:center}
footer{margin-top:28px;font-size:12px;color:var(--muted);text-align:center}
.jump{display:none}
@media (hover:hover){li a:hover{background:var(--accent-soft)}li a:hover .headline{text-decoration:underline;text-underline-offset:3px}.filters button:hover{color:var(--text)}.more:hover{background:var(--card)}}
@media (min-width:900px){
body{display:grid;grid-template-columns:270px minmax(0,720px);justify-content:center;column-gap:48px;padding:0 32px}
header{position:sticky;top:0;align-self:start;height:100vh;overflow-y:auto;padding:40px 0 24px;border-bottom:0;background:transparent}
h1{font-size:25px}
.subrow{margin:8px 0 26px}
.jump-select{display:none}
.filters{flex-direction:column;gap:4px;overflow:visible;margin:0;padding:0}
.filters button{display:flex;justify-content:space-between;align-items:center;width:100%;text-align:left;border-color:transparent;background:transparent;border-radius:10px;padding:9px 12px;font-size:15px}
.filters button[aria-pressed="true"]{background:var(--accent-soft)}
.controls{flex-direction:column;margin-top:22px}
.controls input,.controls select{width:100%;flex:none}
.jump{display:block;margin-top:28px;padding-top:20px;border-top:1px solid var(--line)}
.jump p{margin:0 0 8px 12px;font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.jump a{display:block;padding:6px 12px;border-radius:8px;font-size:14px;color:var(--muted);text-decoration:none}
.jump a:hover{color:var(--text);background:var(--card)}
.jump a[hidden]{display:none}
main{max-width:none;margin:0;padding:28px 0 48px}
.month{scroll-margin-top:24px}
li a{padding:14px 18px}
.headline{font-size:17px}
}
"""

INDEX_JS = """
(function(){
const LIMIT=8;
const items=[...document.querySelectorAll('.item')],months=[...document.querySelectorAll('.month')];
const q=document.getElementById('q'),src=document.getElementById('src'),win=document.getElementById('win');
const count=document.getElementById('count'),none=document.getElementById('none'),jump=document.querySelector('.jump-select');
const expanded=new Set();
let cat='All';
document.querySelectorAll('.filters button').forEach(b=>b.addEventListener('click',()=>{cat=b.dataset.cat;
document.querySelectorAll('.filters button').forEach(x=>x.setAttribute('aria-pressed',x===b));expanded.clear();apply();window.scrollTo(0,0);}));
function apply(){
const term=q.value.trim().toLowerCase(),s=src.value,w=+win.value,now=Date.now();
const matches=li=>(cat==='All'||li.dataset.cat===cat)&&(s==='All'||li.dataset.src===s)&&
(!w||now-(+li.dataset.ts)<=w*864e5)&&(!term||li.dataset.text.includes(term));
let total=0;
months.forEach(m=>{
const lis=[...m.querySelectorAll('li.item')],match=lis.filter(matches);
const open=expanded.has(m.id);
const limit=match.length<=LIMIT+2?match.length:LIMIT;
let shown=0;
lis.forEach(li=>{const pos=match.indexOf(li),show=pos!==-1&&(open||pos<limit);
li.hidden=!show;li.classList.toggle('first-visible',show&&shown===0);if(show)shown++;});
m.hidden=match.length===0;
m.querySelector('h2 span').textContent=match.length;
const btn=m.querySelector('.more');
btn.hidden=!open&&match.length-limit<=0;
btn.textContent=open?'Show less':'Show more';
const link=document.querySelector('.jump a[href="#'+m.id+'"]');if(link)link.hidden=match.length===0;
const opt=jump&&jump.querySelector('option[value="'+m.id+'"]');if(opt)opt.disabled=match.length===0;
total+=match.length;});
none.hidden=total>0;
count.textContent=total+(total===1?' story':' stories');}
document.querySelectorAll('.more').forEach(btn=>btn.addEventListener('click',()=>{
const m=btn.closest('.month');
if(expanded.has(m.id)){expanded.delete(m.id);apply();m.scrollIntoView();}else{expanded.add(m.id);apply();}}));
if(jump)jump.addEventListener('change',()=>{const m=document.getElementById(jump.value);if(m)m.scrollIntoView({behavior:'smooth'});jump.value='';});
[q,src,win].forEach(e=>{e.addEventListener('input',apply);e.addEventListener('change',apply);});
apply();})();
"""


def shell(title, body, script="", css=CSS, wrap=True):
    inner = f'<div class="wrap">{body}</div>' if wrap else body
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{html.escape(title)}</title>{FONTS}<style>{css}</style></head>'
            f'<body>{inner}'
            f'{f"<script>{script}</script>" if script else ""}</body></html>')


def month_label(ym):
    return dt.date.fromisoformat(ym + "-01").strftime("%B %Y")


def render_index(items, brief_date):
    e = html.escape
    updated = NOW.strftime("%a, %b %-d, %H:%M UTC")
    srcs = {}
    for i in items:
        srcs[i["source"]] = srcs.get(i["source"], 0) + 1
    top_src = [s for s, n in sorted(srcs.items(), key=lambda x: -x[1]) if n >= 2][:40]
    chips = ['<button data-cat="All" aria-pressed="true">All</button>'] + [
        f'<button data-cat="{e(c)}" aria-pressed="false">{e(c)}</button>'
        for c in CATEGORIES + ["Industry"]]
    opts = "".join(f'<option>{e(s)}</option>' for s in sorted(top_src))
    brief_link = (f'<p class="brief-row"><a class="brieflink" href="brief.html">'
                  f'Read the brief for {e(brief_date)}</a></p>' if brief_date else "")

    # Group items by month, newest first (items are already sorted that way).
    months, groups = [], {}
    for i in items:
        ym = i["date"][:7]
        if ym not in groups:
            groups[ym] = []
            months.append(ym)
        when = dt.datetime.fromisoformat(i["date"])
        ts = int(when.timestamp() * 1000)
        text = e(f'{i["title"]} {i["source"]} {i["summary"]}'.lower())
        groups[ym].append(
            f'<li class="item" style="--cc:var({CAT_VAR[i["category"]]})" data-cat="{e(i["category"])}" '
            f'data-src="{e(i["source"])}" data-ts="{ts}" data-text="{text}">'
            f'<a href="{e(i["link"])}" target="_blank" rel="noopener">'
            f'<div class="tag">{e(i["category"])}</div>'
            f'<div class="headline">{e(i["title"])}</div>'
            f'<div class="meta">{e(i["source"])} · '
            f'<time datetime="{e(i["date"])}">{when.strftime("%b %-d")}</time></div></a></li>')
    sections = "".join(
        f'<section class="month" id="m-{ym}"><h2>{month_label(ym)} <span></span></h2>'
        f'<ul>{"".join(groups[ym])}</ul><button class="more" hidden></button></section>'
        for ym in months)
    jump_opts = '<option value="">Jump to month</option>' + "".join(
        f'<option value="m-{ym}">{month_label(ym)}</option>' for ym in months)
    jump_links = "".join(f'<a href="#m-{ym}">{month_label(ym)}</a>' for ym in months)

    body = (f'<header><div class="brand"><h1>Diagnostics Desk</h1></div>'
            f'<div class="subrow"><p class="updated">Updated {updated}</p>'
            f'<select class="jump-select" aria-label="Jump to month">{jump_opts}</select></div>'
            f'<div class="filters">{"".join(chips)}</div>'
            f'<div class="controls"><input id="q" type="search" placeholder="Search headlines" '
            f'aria-label="Search headlines"><select id="src" aria-label="Source"><option>All</option>{opts}</select>'
            f'<select id="win" aria-label="Time window"><option value="1">Last 24 hours</option>'
            f'<option value="7" selected>Last 7 days</option><option value="30">Last 30 days</option>'
            f'<option value="0">All stored</option></select></div>'
            f'<p id="count"></p>{brief_link}'
            f'<nav class="jump" aria-label="Jump to month"><p>Jump to</p>{jump_links}</nav></header>'
            f'<main>{sections}<p class="empty" id="none" hidden>No stories match these filters.</p>'
            f'<footer>Headlines link to the original publishers. Refreshed daily.</footer></main>')
    return shell("Diagnostics Desk", body, INDEX_JS, INDEX_CSS, wrap=False)


def render_brief(md_text, date, all_dates):
    body_html = markdown.markdown(md_text, extensions=["extra"])
    body_html = body_html.replace("<a href=", '<a target="_blank" rel="noopener" href=')
    past = "".join(f'<a href="briefs/{d}.html">{d}</a>' for d in all_dates if d != date)
    past_html = f'<div class="past">Earlier briefs: {past}</div>' if past else ""
    nav = '<p><a href="index.html">Back to the feed</a></p>'
    return shell(f"Diagnostics Brief {date}", f'{nav}<article class="brief">{body_html}</article>{past_html}')


# ------------------------------------------------------------------ MAIN ---


def main():
    store = load_store()
    days = 14 if not store else 3           # first run backfills two weeks
    fresh = fetch_google(days) + fetch_pubmed() + fetch_trials()
    for label, url in TRADE_FEEDS.items():
        fresh += fetch_rss(url, label, relevance=True)
    for item in fresh:
        store.setdefault(item["id"], item)
    items = save_store(store)
    print(f"[done] {len(fresh)} fetched, {len(items)} stored")

    today = NOW.strftime("%Y-%m-%d")
    BRIEFS.mkdir(exist_ok=True)
    md_path = BRIEFS / f"{today}.md"
    if md_path.exists() and not os.getenv("FORCE_BRIEF"):
        print("[skip] brief already exists for today")
    else:
        md = make_brief(brief_candidates(items), today)
        if md:
            md_path.write_text(md)

    dates = sorted((p.stem for p in BRIEFS.glob("*.md")), reverse=True)
    for d in dates:
        text = (BRIEFS / f"{d}.md").read_text()
        (BRIEFS / f"{d}.html").write_text(render_brief(text, d, dates).replace(
            'href="index.html"', 'href="../index.html"').replace('href="briefs/', 'href="'))
    if dates:
        latest = (BRIEFS / f"{dates[0]}.md").read_text()
        (ROOT / "brief.html").write_text(render_brief(latest, dates[0], dates))
    (ROOT / "index.html").write_text(render_index(items, dates[0] if dates else None))


if __name__ == "__main__":
    main()
