"""Read a recipe link from an "Add a recipe" issue, sort it, and append it to added.js.

Runs in GitHub Actions (see .github/workflows/add-recipe.yml). Also runs locally:
    ISSUE_BODY="### Recipe link\n\nhttps://..." python .github/scripts/import_recipe.py

Writes comment.md (the reply posted on the issue) and sets the step outputs
status=added|failed and commit=<commit message>.
"""
import datetime
import html
import json
import os
import re
import sys
from urllib.parse import urlparse

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ADDED = os.path.join(ROOT, "added.js")
INDEX = os.path.join(ROOT, "index.html")
SITE = "https://rsissons.github.io/grill-recipes/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def out(key, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{key}={value}\n")
    else:
        print(f"[output] {key}={value}")


def finish(status, comment, commit=""):
    with open(os.path.join(ROOT, "comment.md"), "w", encoding="utf-8") as f:
        f.write(comment)
    out("status", status)
    if commit:
        out("commit", commit.replace("\n", " "))
    print(comment)
    sys.exit(0)


def fail(reason):
    finish("failed", f"Couldn't add that one: {reason}\n\nNothing was changed on the site.")


# ---------------- parse the issue form ----------------
def parse_form(body):
    fields, cur = {}, None
    body = re.sub(r"<!--.*?-->", "", body or "", flags=re.S)
    for line in body.splitlines():
        m = re.match(r"^###\s+(.+?)\s*$", line)
        if m:
            cur = m.group(1).lower()
            fields[cur] = ""
        elif cur is not None:
            fields[cur] += line + "\n"
    clean = {}
    for k, v in fields.items():
        v = v.strip()
        clean[k] = "" if v in ("_No response_", "Auto-detect") else v
    return clean


# ---------------- fetch and extract ----------------
def clean_text(s):
    s = html.unescape(re.sub(r"<[^>]+>", " ", str(s or "")))
    return re.sub(r"\s+", " ", s).strip()


def find_recipe(node):
    if isinstance(node, list):
        for n in node:
            r = find_recipe(n)
            if r:
                return r
    elif isinstance(node, dict):
        t = node.get("@type")
        types = t if isinstance(t, list) else [t]
        if "Recipe" in types:
            return node
        for key in ("@graph", "mainEntity", "itemListElement"):
            if key in node:
                r = find_recipe(node[key])
                if r:
                    return r
    return None


def flatten_steps(instr):
    steps = []
    if isinstance(instr, str):
        parts = re.split(r"(?:\r?\n)+|(?<=\.)\s+(?=\d+\.\s)", instr)
        steps += [clean_text(p) for p in parts]
    elif isinstance(instr, list):
        for it in instr:
            steps += flatten_steps(it)
    elif isinstance(instr, dict):
        if "itemListElement" in instr:
            steps += flatten_steps(instr["itemListElement"])
        else:
            steps.append(clean_text(instr.get("text") or instr.get("name") or ""))
    return [s for s in steps if s]


def iso_minutes(v):
    if not v or not isinstance(v, str):
        return 0
    m = re.match(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?", v.strip())
    if not m:
        return 0
    d, h, mi = (int(x) if x else 0 for x in m.groups())
    return d * 1440 + h * 60 + mi


def as_text(v):
    if isinstance(v, list):
        return ", ".join(as_text(x) for x in v)
    if isinstance(v, dict):
        return as_text(v.get("name", ""))
    return clean_text(v)


HEADER_SETS = [
    {"User-Agent": "Mozilla/5.0"},
    {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml", "Accept-Language": "en-US,en;q=0.9"},
]


def get(url, **kw):
    try:
        return requests.get(url, timeout=kw.pop("timeout", 30), **kw)
    except requests.RequestException:
        return None


def fetch_recipe(url):
    """Return (page, recipe, via). Tries the site directly, then the Internet Archive's copy."""
    codes, page_seen = [], None
    for h in HEADER_SETS:
        r = get(url, headers=h)
        if r is None:
            codes.append("no answer")
            continue
        codes.append(str(r.status_code))
        if r.status_code == 200:
            page_seen = r.text
            rec = extract(r.text)
            if rec:
                return r.text, rec, "site"
    # Many sites (Cloudflare) block cloud servers like GitHub's. A public reader fetches the page for us.
    r = get("https://r.jina.ai/" + url, headers={"X-Return-Format": "html"}, timeout=60)
    if r is not None and r.status_code == 200:
        rec = extract(r.text)
        if rec:
            return r.text, rec, "reader"
    snap = None
    r = get("https://archive.org/wayback/available", params={"url": url})
    if r is not None and r.status_code == 200:
        try:
            snap = r.json().get("archived_snapshots", {}).get("closest")
        except ValueError:
            snap = None
    if snap and snap.get("timestamp"):
        r = get(f"https://web.archive.org/web/{snap['timestamp']}id_/{url}", timeout=45)
        if r is not None and r.status_code == 200:
            rec = extract(r.text)
            if rec:
                return r.text, rec, "archive"
    if page_seen is not None:
        rec = extract_text(page_seen)
        if rec:
            return page_seen, rec, "text"
        fail("that page has no recipe card and no Ingredients and Directions lists I could read, so it doesn't look like a recipe.")
    fail(f"the site blocked the automated reader (answered {', '.join(codes)}) and the Internet Archive has no readable copy.")


JUNK_LINE = re.compile(r"^(view cart|checkout|add to cart|print|pin|share|jump to|save|\(\d+\)|\d+ reviews?)\b", re.I)
END_HEAD = re.compile(r"^(find similar articles|recipe video|notes?|nutrition|share|related|you may also like|recommended|more recipes|comments?|leave a (comment|review))\b", re.I)


def extract_text(page):
    """No recipe card? Read plain "Ingredients" and "Directions" lists (older Blackstone posts, many blogs)."""
    body = re.sub(r"<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", page, flags=re.S | re.I)
    body = re.sub(r"<br\s*/?>|</(p|li|h\d|div|tr)>", "\n", body, flags=re.I)
    lines = [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", x))).strip() for x in body.split("\n")]
    lines = [x for x in lines if x]
    ing_at = next((i for i, x in enumerate(lines) if re.fullmatch(r"ingredients:?", x, re.I)), None)
    if ing_at is None:
        return None
    dir_at = next((i for i in range(ing_at + 1, len(lines)) if re.fullmatch(r"(directions|instructions|method|steps|preparation):?", lines[i], re.I)), None)
    if dir_at is None:
        return None
    ings = [x for x in lines[ing_at + 1:dir_at] if not JUNK_LINE.match(x) and len(x) < 200 and not x.endswith(":")]
    steps = []
    for x in lines[dir_at + 1:dir_at + 60]:
        if END_HEAD.match(x) or x.lower().startswith("©"):
            break
        if JUNK_LINE.match(x) or len(x) < 12:
            continue
        steps.append(x)
    if len(ings) < 2 or not steps:
        return None
    m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', page, re.I) or re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S | re.I)
    name = clean_text(m.group(1)) if m else ""
    name = re.split(r"\s+[|–-]\s+", name)[0]
    head = " ".join(lines[max(0, ing_at - 6):ing_at])
    rec = {"name": name, "recipeIngredient": ings, "recipeInstructions": steps}
    sv = re.search(r"\bserves\s+(\d+)", head, re.I)
    if sv:
        rec["recipeYield"] = sv.group(1)
    tm = re.search(r"(?:time|cook time)\s*:?\s*((?:\d+\s*hours?\s*)?(?:\d+\s*min(?:ute)?s?)?)", head, re.I)
    if tm and tm.group(1).strip():
        h = re.search(r"(\d+)\s*hour", tm.group(1)); mi = re.search(r"(\d+)\s*min", tm.group(1))
        rec["totalTime"] = f"PT{int(h.group(1)) if h else 0}H{int(mi.group(1)) if mi else 0}M"
    return rec


def extract(page):
    for block in re.findall(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", page, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except json.JSONDecodeError:
            try:
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", block.strip()))
            except json.JSONDecodeError:
                continue
        rec = find_recipe(data)
        if rec:
            return rec
    return None


def site_name(page, rec, url):
    pub = rec.get("publisher")
    if isinstance(pub, dict) and pub.get("name"):
        return clean_text(pub["name"])
    m = re.search(r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)', page, re.I)
    if m:
        return clean_text(m.group(1))
    return urlparse(url).netloc.replace("www.", "")


# ---------------- sorting ----------------
BEEF = r"\b(beef|steak|brisket|burgers?|cheeseburgers?|ribeye|rib eye|sirloin|tri-?tip|short ribs?|carne asada|bulgogi|galbi|barbacoa|meatloaf|tenderloin|chuck|prime rib|kofta|pastrami|flank|skirt|picanha|cheesesteak|philly|london broil|porterhouse|t-bone|tomahawk|filet mignon|meatballs?)\b"
CHICKEN = r"\b(chicken|wings?|thighs?|drumsticks?|poultry|pollo|turkey|hen)\b"
PORK = r"\b(pork|ribs|pulled pork|carnitas|al pastor|sausages?|kielbasa|brats?|bratwurst|ham|pork belly|samgyeopsal|chorizo)\b"
SEAFOOD = r"\b(shrimp|prawns?|salmon|fish|tuna|ahi|mahi|cod|halibut|tilapia|snapper|grouper|trout|swordfish|catfish|walleye|scallops?|lobster|crab|clams?|mussels|oysters?|seafood)\b"
OTHER = r"\b(lamb|tofu|duck|venison|bison|goat)\b"
APP_WORDS = r"\b(dip|poppers?|bites|nachos|crostini|bruschetta|sliders|appetizers?|starters?|snacks?|queso|deviled|stuffed mushrooms|skewers? appetizer|flatbread|guacamole|salsa)\b"
SIDE_WORDS = r"\b(salad|slaw|potatoes|fries|rice|beans|corn|vegetables|veggies|asparagus|broccoli|brussels|zucchini|squash|mac and cheese|mac n|coleslaw|greens|carrots|mushrooms|side)\b"

REGIONS = [
    ("Thai", r"\b(thai|pad thai|satay|lemongrass|tom yum|larb|kra pao|panang)\b"),
    ("Korean", r"\b(korean|gochujang|gochugaru|bulgogi|galbi|kalbi|kimchi|bibimbap)\b"),
    ("Japanese", r"\b(japanese|teriyaki|hibachi|teppanyaki|yakitori|miso|ponzu|katsu|furikake|shishito)\b"),
    ("Chinese", r"\b(chinese|kung pao|lo mein|chow mein|szechuan|sichuan|general tso|orange chicken|char siu|hoisin|five[- ]spice|mongolian|fried rice|dumplings?|potstickers?)\b"),
    ("Indian", r"\b(indian|tandoori|tikka|masala|curry|garam|naan|biryani)\b"),
    ("Hawaiian", r"\b(hawaiian|huli|luau|kalua|loco moco)\b"),
    ("Caribbean", r"\b(jerk|caribbean|jamaican|cuban|mojo)\b"),
    ("Mexican", r"\b(mexican|tex-mex|tacos?|fajitas?|carne asada|elote|al pastor|chipotle|quesadillas?|birria|enchiladas?|burritos?|tamales?|salsa|guacamole|cotija|tajin|chimichanga)\b"),
    ("South American", r"\b(chimichurri|peruvian|brazilian|argentin\w*|picanha|churrasco|aji)\b"),
    ("African", r"\b(suya|piri[- ]piri|peri[- ]peri|moroccan|harissa|berbere|ethiopian)\b"),
    ("Mediterranean", r"\b(greek|gyros?|souvlaki|tzatziki|feta|kofta|shawarma|halloumi|mediterranean|hummus|lebanese|turkish|za'atar)\b"),
    ("European", r"\b(italian|parmesan|pesto|caprese|marinara|bruschetta|tuscan|french|spanish|german|british|pizza|carbonara|alfredo)\b"),
    ("Cajun", r"\b(cajun|creole|andouille|blackened|jambalaya|gumbo|louisiana|new orleans)\b"),
    ("Southern", r"\b(southern|nashville|collard|biscuits?|fried chicken|grits|cornbread|pimento)\b"),
    ("BBQ", r"\b(bbq|barbecue|brisket|burnt ends|smoked|pulled pork|dry rub|pitmaster)\b"),
]
REGION_NAMES = {name.lower(): name for name, _ in REGIONS} | {"american": "American"}


def score(pattern, text, title):
    return len(re.findall(pattern, text)) + 3 * len(re.findall(pattern, title))


def pick_cooker(title, text):
    s = {
        "BS": score(r"\b(blackstone|griddle|flat[- ]?top|flattop|hibachi|teppanyaki|smash(ed)? burgers?|wok|stir[- ]?fr(y|ied)|skillet|saut[eé])\b", text, title),
        "PBC": score(r"\b(pit barrel|pbc|smoker|smoked|smoking|drum smoker|low and slow|wood chunks|pellet grill|burnt ends|hooks?)\b", text, title),
        "GR": score(r"\b(grill|grilled|grilling|grates|kebabs?|kabobs?|skewers?|charcoal)\b", text, title),
        "SV": score(r"\b(sous[- ]vide|immersion circulator|water bath|anova|joule)\b", text, title) * 3,
        "SC": score(r"\b(slow[- ]cooker|crock[- ]?pot|crockpot)\b", text, title) * 3,
        "AF": score(r"\b(air[- ]?fr(y|yer|yers|ied|ying))\b", text, title) * 3,
    }
    best = max(s, key=s.get)
    return (best, False) if s[best] > 0 else ("GR", True)


def pick_type(title, cat, ings):
    first = " ".join(ings[:4]).lower()
    if re.search(r"\b(appetizer|snack|starter|dip|finger food|hors d)", cat):
        return "Appetizer"
    if re.search(r"\bside\b", cat):
        return "Side"
    if re.search(APP_WORDS, title):
        return "Appetizer"
    beefy = re.search(r"\b(beef|steak|burgers?|cheeseburgers?|brisket)\b", title)
    chickeny = re.search(CHICKEN, title)
    # An explicit seafood/pork word wins over ambiguous cuts like "tenderloin" or "ribs"
    if not beefy and not chickeny:
        if re.search(SEAFOOD, title):
            return "Seafood"
        if re.search(PORK, title):
            return "Pork"
        if re.search(OTHER, title):
            return "Other"
    if re.search(BEEF, title):
        return "Beef"
    if chickeny:
        return "Chicken"
    if re.search(SEAFOOD, title):
        return "Seafood"
    main_cat = re.search(r"\b(main|dinner|entr[eé]e|supper)", cat)
    if re.search(SIDE_WORDS, title) and not main_cat:
        return "Side"
    for pat, t in ((BEEF, "Beef"), (CHICKEN, "Chicken"), (SEAFOOD, "Seafood"), (PORK, "Pork"), (OTHER, "Other")):
        if re.search(pat, first):
            return t
    # Protein further down the list. Skip seasoning lines like "chicken stock" or "shrimp paste".
    rest = " ".join(i for i in ings if not re.search(r"\b(stock|broth|bouillon|paste|sauce|powder|seasoning|fat|drippings)\b", i.lower())).lower()
    for pat, t in ((BEEF, "Beef"), (CHICKEN, "Chicken"), (SEAFOOD, "Seafood"), (PORK, "Pork"), (OTHER, "Other")):
        if re.search(pat, rest):
            return t
    return "Other" if main_cat else "Side"


def pick_region(cuisine, title, text):
    for name, pat in REGIONS:
        if re.search(pat, title):
            return name
    for c in re.split(r"[,/]", cuisine.lower()):
        c = c.strip()
        if not c or c in ("american", "usa", "us", "north american", "comfort food"):
            continue
        for key, name in REGION_NAMES.items():
            if key in c:
                return name
        if c in ("tex-mex", "latin", "latin american"):
            return "Mexican"
        if c in ("italian", "french", "spanish", "german", "british", "portuguese"):
            return "European"
        if c in ("greek", "middle eastern", "lebanese", "turkish"):
            return "Mediterranean"
    best, hits = "American", 0
    for name, pat in REGIONS:
        n = len(re.findall(pat, text))
        if n > hits:
            best, hits = name, n
    return best if hits >= 2 else "American"


# ---------------- macros ----------------
def first_num(v):
    m = re.search(r"\d+(?:\.\d+)?", str(v or ""))
    return float(m.group(0)) if m else None


def servings_of(y):
    for v in (y if isinstance(y, list) else [y]):
        n = first_num(v)
        if n:
            return int(n)
    return None


def macros_of(rec):
    n = rec.get("nutrition")
    if not isinstance(n, dict):
        return None
    vals = [first_num(n.get(k)) for k in ("calories", "proteinContent", "carbohydrateContent", "fatContent")]
    if any(v is None for v in vals) or not vals[0]:
        return None
    cal, pr, cb, ft = (round(v) for v in vals)
    return {"cal": cal, "p": pr, "c": cb, "f": ft, "s": servings_of(rec.get("recipeYield")) or 4, "from": "s",
            "note": clean_text(n.get("servingSize")) if n.get("servingSize") and not re.fullmatch(r"\s*1\s*(serving|g)?\s*", str(n.get("servingSize"))) else ""}


# ---------------- existing data ----------------
def load_added():
    if not os.path.exists(ADDED):
        return []
    raw = open(ADDED, encoding="utf-8").read()
    m = re.search(r"window\.ADDED\s*=\s*(\[.*\])\s*;", raw, re.S)
    return json.loads(m.group(1)) if m else []


def save_added(items):
    with open(ADDED, "w", encoding="utf-8", newline="\n") as f:
        f.write('/* Recipes added through the "Add a recipe" form. Written by .github/scripts/import_recipe.py. */\n')
        f.write("window.ADDED = " + json.dumps(items, indent=1, ensure_ascii=False) + ";\n")


STOP = {"the", "best", "easy", "recipe", "homemade", "perfect", "simple", "ultimate", "authentic", "quick",
        "classic", "my", "a", "an", "and", "with", "on", "for", "of", "how", "to", "make", "copycat",
        "blackstone", "griddle", "grilled", "smoked", "pit", "barrel", "style"}


COOKER_WORDS = re.compile(r"\b(air[- ]?fr(y|yer|ied|ying)|sous[- ]?vide|slow[- ]?cooker|crock[- ]?pot|crockpot|instant[- ]?pot|pressure[- ]?cooker|flat[- ]?top|on the (grill|griddle|smoker)|pellet grill|traeger)\b")


def norm_name(n):
    n = COOKER_WORDS.sub(" ", n.lower())
    words = re.findall(r"[a-z0-9]+", re.sub(r"\([^)]*\)", " ", n))
    return " ".join((w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w) for w in words if w not in STOP)


def slug(n):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", n.lower())).strip("-")[:60] or "recipe"


def main():
    form = parse_form(os.environ.get("ISSUE_BODY", ""))
    url = (form.get("recipe link") or "").strip().strip("<>")
    m = re.search(r"https?://\S+", url)
    if not m:
        fail("I couldn't find a web link in the form.")
    url = m.group(0).rstrip(").,")

    page, rec, via = fetch_recipe(url)

    page_name = clean_text(rec.get("name")) or "Untitled recipe"
    name_in = re.sub(r"[^\w\s&',()-]", "", form.get("name (optional)", "")).strip()[:60]
    name = name_in or page_name
    ings = [clean_text(x) for x in (rec.get("recipeIngredient") or rec.get("ingredients") or [])]
    ings = [x for x in ings if x][:30]
    steps = flatten_steps(rec.get("recipeInstructions"))[:20]
    if not ings or not steps:
        fail("the page's recipe data is missing the ingredients or the steps.")

    mins = iso_minutes(rec.get("totalTime")) or (iso_minutes(rec.get("prepTime")) + iso_minutes(rec.get("cookTime"))) or 45
    desc = clean_text(rec.get("description"))
    if len(desc) > 180:
        desc = desc[:180].rsplit(" ", 1)[0] + "…"
    cuisine, cat, kw = as_text(rec.get("recipeCuisine")), as_text(rec.get("recipeCategory")).lower(), as_text(rec.get("keywords"))
    title = name.lower()
    text = " ".join([title, desc.lower(), kw.lower(), cat, cuisine.lower(), " ".join(steps).lower(), " ".join(ings).lower()])

    cooker_pick = {"blackstone": "BS", "pit barrel": "PBC", "pit barrel / smoker": "PBC", "grill": "GR", "sous vide": "SV", "slow cooker": "SC", "air fryer": "AF"}.get(form.get("cooker", "").lower())
    guessed_cooker = False
    if not cooker_pick:
        cooker_pick, guessed_cooker = pick_cooker(title, text)
        # Blackstone's own sites are griddle recipes unless the page is clearly for another cooker
        if cooker_pick in ("GR", "PBC") and re.search(r"blackstoneproducts\.com|griddlesizzle\.com", url):
            cooker_pick, guessed_cooker = "BS", False
    finish_cooker = None
    if cooker_pick in ("SV", "SC"):   # combos: which cooker does the final sear/crisp/smoke?
        fin = {k: score(pat, text, title) for k, pat in (
            ("BS", r"\b(blackstone|griddle|flat[- ]?top|skillet|cast iron|pan[- ]?sear)"),
            ("PBC", r"\b(pit barrel|smoker|smoked|smoke)\b"),
            ("GR", r"\b(grill|grilled|grilling|grates|charcoal)\b"))}
        best = max(fin, key=fin.get)
        finish_cooker = best if fin[best] > 0 else None
    type_pick = {"beef main": "Beef", "chicken main": "Chicken", "pork main": "Pork", "seafood main": "Seafood",
                 "other main (lamb, duck, tofu)": "Other",
                 "side dish": "Side", "appetizer": "Appetizer"}.get(form.get("type", "").lower()) or pick_type(title, cat, ings)
    region_in = form.get("region (optional)", "").strip()
    region = REGION_NAMES.get(region_in.lower()) or (region_in.title() if region_in else pick_region(cuisine, title, text))

    items = load_added()
    idx = open(INDEX, encoding="utf-8").read()
    builtin_names = re.findall(r'\{id:"[^"]+",name:"([^"]+)",cooker:"(\w+)"', idx)
    builtin_ids = set(re.findall(r'\{id:"([^"]+)"', idx))

    replacing = next((i for i, r in enumerate(items) if r.get("src", ["", ""])[1] == url), None)
    nn = norm_name(name)
    # same dish on the same cooker is a duplicate; the same dish on a different cooker is allowed (the site links them)
    for bn, bc in builtin_names:
        if norm_name(bn) == nn and bc == cooker_pick:
            fail(f"**{name}** looks like a dish that's already on the site (**{bn}**)."
                 + ("" if name_in else " If it's a different take, submit it again with your own **Name** (e.g. with the main ingredient in it)."))
    for i, r in enumerate(items):
        if i != replacing and norm_name(r["name"]) == nn and r.get("cooker") == cooker_pick:
            fail(f"**{name}** is already on the site (added earlier from {r['src'][1]})."
                 + ("" if name_in else " If it's a different dish, submit it again with your own **Name**."))

    taken = builtin_ids | {r["id"] for i, r in enumerate(items) if i != replacing}
    rid, n = slug(name), 2
    base = rid
    while rid in taken:
        rid, n = f"{base}-{n}", n + 1

    recipe = {"id": rid, "name": name, "cooker": cooker_pick, "type": type_pick, "region": region,
              "mins": mins, "serves": servings_of(rec.get("recipeYield")) or 4, "desc": desc, "ing": ings, "steps": steps, "tip": "",
              "src": [f"{site_name(page, rec, url)}: {page_name}", url],
              "added_on": datetime.date.today().isoformat()}
    if finish_cooker:
        recipe["finish"] = finish_cooker
    mac = macros_of(rec)
    if mac:
        recipe["mac"] = mac
    ar = rec.get("aggregateRating")
    ar = ar[0] if isinstance(ar, list) and ar else ar
    if isinstance(ar, dict) and "pitbarrelcooker.com" not in url:
        rv, rn = first_num(ar.get("ratingValue")), first_num(ar.get("ratingCount") or ar.get("reviewCount"))
        if rv and rn and 0 < rv <= 5:
            recipe["rating"] = {"v": round(rv, 1), "n": int(rn)}
    if replacing is not None:
        # a resubmit keeps the earlier cooker/type/region unless the form sets them this time
        old = items[replacing]
        recipe["added_on"] = old.get("added_on", recipe["added_on"])
        if old.get("curated"):
            recipe["curated"] = 1
        if not form.get("cooker"):
            recipe["cooker"] = old.get("cooker", recipe["cooker"])
            recipe.pop("finish", None)
            if old.get("finish"):
                recipe["finish"] = old["finish"]
        if not form.get("type"):
            recipe["type"] = old.get("type", recipe["type"])
        if not form.get("region (optional)"):
            recipe["region"] = old.get("region", recipe["region"])
        items[replacing] = recipe
    else:
        items.append(recipe)
    save_added(items)

    cookers = {"BS": "Blackstone", "PBC": "Pit Barrel / Smoker", "GR": "Grill", "SV": "Sous Vide", "SC": "Slow Cooker", "AF": "Air Fryer"}
    types = {"Beef": "Beef main", "Chicken": "Chicken main", "Pork": "Pork main", "Seafood": "Seafood main",
             "Other": "Other main", "Side": "Side dish", "Appetizer": "Appetizer"}
    note = ("\n\nI couldn't tell which cooker this is for, so it's filed under **Grill**. "
            "To change it, submit the link again and pick the cooker in the form.") if guessed_cooker else ""
    verb = "Updated" if replacing is not None else "Added"
    comment = (f"{verb} **{name}**\n\n"
               f"- Cooker: **{cookers[recipe['cooker']]}{(' + ' + cookers[recipe['finish']]) if recipe.get('finish') else ''}**\n- Type: **{types[recipe['type']]}**\n- Region: **{recipe['region']}**\n"
               f"- Time: about {mins} min · {len(ings)} ingredients · {len(steps)} steps\n"
               + (f"- Macros per serving (from the site): {mac['cal']} cal · {mac['p']}g protein · {mac['c']}g carbs · {mac['f']}g fat\n" if mac else "- Macros: the site doesn't publish them\n")
               + ("- Read from the Internet Archive's copy (the site blocks automated readers)\n" if via == "archive" else "")
               + f"\nLive in a few minutes (refresh the page): {SITE}#{rid}{note}\n\n"
               "Sorted wrong? Submit the same link again and set the Cooker, Type or Region in the form. "
               "It replaces this entry.")
    finish("added", comment, f"{verb} recipe: {name}")


if __name__ == "__main__":
    main()
