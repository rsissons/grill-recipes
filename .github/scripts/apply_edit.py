"""Apply an owner edit to a recipe: rename, re-sort, change the time, hide (delete) or restore.

Edits live in overrides.js (window.OVERRIDES = {id: {...}}), which the site applies on load to
built-in and imported recipes alike, so nothing in the recipe data itself is rewritten and every
change can be undone. The relay only files these issues after checking the owner's code.
"""
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OVR = os.path.join(ROOT, "overrides.js")
COOKERS = {"BS", "PBC", "GR", "SV", "SC", "AF"}
TYPES = {"Beef", "Chicken", "Pork", "Seafood", "Other", "Side", "Appetizer"}


def out(**kv):
    with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a", encoding="utf-8") as f:
        for k, v in kv.items():
            f.write(f"{k}={v}\n")


def finish(status, msg, commit=""):
    open("comment.md", "w", encoding="utf-8").write(msg + "\n")
    out(status=status, commit=commit)
    print(msg)
    raise SystemExit(0)


def load():
    if not os.path.exists(OVR):
        return {}
    s = open(OVR, encoding="utf-8").read()
    return json.loads(s[s.index("{"):s.rindex("}") + 1])


def main():
    body = os.environ.get("ISSUE_BODY", "")
    m = re.search(r"```json\s*(\{.*?\})\s*```", body, re.S)
    if not m:
        finish("failed", "Couldn't read that edit.")
    try:
        e = json.loads(m.group(1))
    except ValueError:
        finish("failed", "Couldn't read that edit.")
    rid = str(e.get("id", ""))
    if not re.fullmatch(r"[a-z0-9-]{1,80}", rid):
        finish("failed", "That recipe id isn't valid.")
    ovr = load()
    cur = ovr.get(rid, {})
    if e.get("restore"):
        cur.pop("hidden", None)
        verb = "Restored"
    elif e.get("remove"):
        cur["hidden"] = True
        verb = "Deleted"
    else:
        st = e.get("set") or {}
        if "name" in st:
            n = re.sub(r"\s+", " ", str(st["name"])).strip()[:60]
            if len(n) < 3:
                finish("failed", "The name is too short.")
            cur["name"] = n
        if "cooker" in st:
            if st["cooker"] not in COOKERS:
                finish("failed", "Unknown cooker.")
            cur["cooker"] = st["cooker"]
        if "type" in st:
            if st["type"] not in TYPES:
                finish("failed", "Unknown type.")
            cur["type"] = st["type"]
        if "region" in st:
            rg = str(st["region"]).strip()
            if not re.fullmatch(r"[A-Za-z ]{3,24}", rg):
                finish("failed", "That region isn't valid.")
            cur["region"] = rg
        if "mins" in st:
            try:
                mn = int(st["mins"])
            except (TypeError, ValueError):
                finish("failed", "The time has to be a number of minutes.")
            if not 1 <= mn <= 4000:
                finish("failed", "The time has to be between 1 and 4000 minutes.")
            cur["mins"] = mn
        verb = "Updated"
    if cur:
        ovr[rid] = cur
    else:
        ovr.pop(rid, None)
    with open(OVR, "w", encoding="utf-8", newline="\n") as f:
        f.write("/* Owner edits to recipes (rename, re-sort, time, delete). Written by .github/scripts/apply_edit.py. */\n")
        f.write("window.OVERRIDES = " + json.dumps(ovr, indent=1, ensure_ascii=False, sort_keys=True) + ";\n")
    finish("added", f"{verb} **{rid}**. Live in a couple of minutes (refresh the page).", f"{verb} recipe: {rid}")


if __name__ == "__main__":
    main()
