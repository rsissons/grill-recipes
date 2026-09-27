"""Read a recipe link from an "Add a recipe" issue, sort it, and append it to added.js.

Runs in GitHub Actions (see .github/workflows/add-recipe.yml). Also runs locally:
    ISSUE_BODY="### Recipe link\n\nhttps://..." python .github/scripts/import_recipe.py

Writes comment.md (the reply posted on the issue) and sets the step outputs
status=added|failed and commit=<commit message>.
"""
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
    for line in (body or "").splitlines():
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
        fail("that page doesn't include standard recipe data (most big recipe sites do; some blogs and store sites don't).")
    fail(f"the site blocked the automated reader (answered {', '.join(codes)}) and the Internet Archive has no readable copy.")


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
    if re.search(SIDE_WORDS, title):
        return "Side"
    for pat, t in ((BEEF, "Beef"), (CHICKEN, "Chicken"), (SEAFOOD, "Seafood"), (PORK, "Pork"), (OTHER, "Other")):
        if re.search(pat, first):
            return t
    return "Side"


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


def norm_name(n):
    words = re.findall(r"[a-z0-9]+", re.sub(r"\([^)]*\)", " ", n.lower()))
    return " ".join(w for w in words if w not in STOP)


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

    name = clean_text(rec.get("name")) or "Untitled recipe"
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

    cooker_pick = {"blackstone": "BS", "pit barrel": "PBC", "grill": "GR"}.get(form.get("cooker", "").lower())
    guessed_cooker = False
    if not cooker_pick:
        cooker_pick, guessed_cooker = pick_cooker(title, text)
    type_pick = {"beef main": "Beef", "chicken main": "Chicken", "pork main": "Pork", "seafood main": "Seafood",
                 "other main (lamb, duck, tofu)": "Other",
                 "side dish": "Side", "appetizer": "Appetizer"}.get(form.get("type", "").lower()) or pick_type(title, cat, ings)
    region_in = form.get("region (optional)", "").strip()
    region = REGION_NAMES.get(region_in.lower()) or (region_in.title() if region_in else pick_region(cuisine, title, text))

    items = load_added()
    idx = open(INDEX, encoding="utf-8").read()
    builtin_names = re.findall(r'\{id:"[^"]+",name:"([^"]+)"', idx)
    builtin_ids = set(re.findall(r'\{id:"([^"]+)"', idx))

    replacing = next((i for i, r in enumerate(items) if r.get("src", ["", ""])[1] == url), None)
    nn = norm_name(name)
    for bn in builtin_names:
        if norm_name(bn) == nn:
            fail(f"**{name}** looks like a dish that's already on the site (**{bn}**).")
    for i, r in enumerate(items):
        if i != replacing and norm_name(r["name"]) == nn:
            fail(f"**{name}** is already on the site (added earlier from {r['src'][1]}).")

    taken = builtin_ids | {r["id"] for i, r in enumerate(items) if i != replacing}
    rid, n = slug(name), 2
    base = rid
    while rid in taken:
        rid, n = f"{base}-{n}", n + 1

    recipe = {"id": rid, "name": name, "cooker": cooker_pick, "type": type_pick, "region": region,
              "mins": mins, "desc": desc, "ing": ings, "steps": steps, "tip": "",
              "src": [f"{site_name(page, rec, url)}: {name}", url]}
    mac = macros_of(rec)
    if mac:
        recipe["mac"] = mac
    if replacing is not None:
        items[replacing] = recipe
    else:
        items.append(recipe)
    save_added(items)

    cookers = {"BS": "Blackstone", "PBC": "Pit Barrel", "GR": "Grill"}
    types = {"Beef": "Beef main", "Chicken": "Chicken main", "Pork": "Pork main", "Seafood": "Seafood main",
             "Other": "Other main", "Side": "Side dish", "Appetizer": "Appetizer"}
    note = ("\n\nI couldn't tell which cooker this is for, so it's filed under **Grill**. "
            "To change it, submit the link again and pick the cooker in the form.") if guessed_cooker else ""
    verb = "Updated" if replacing is not None else "Added"
    comment = (f"{verb} **{name}**\n\n"
               f"- Cooker: **{cookers[cooker_pick]}**\n- Type: **{types[type_pick]}**\n- Region: **{region}**\n"
               f"- Time: about {mins} min · {len(ings)} ingredients · {len(steps)} steps\n"
               + (f"- Macros per serving (from the site): {mac['cal']} cal · {mac['p']}g protein · {mac['c']}g carbs · {mac['f']}g fat\n" if mac else "- Macros: the site doesn't publish them\n")
               + ("- Read from the Internet Archive's copy (the site blocks automated readers)\n" if via == "archive" else "")
               + f"\nLive in a few minutes (refresh the page): {SITE}#{rid}{note}\n\n"
               "Sorted wrong? Submit the same link again and set the Cooker, Type or Region in the form. "
               "It replaces this entry.")
    finish("added", comment, f"{verb} recipe: {name}")


if __name__ == "__main__":
    main()
