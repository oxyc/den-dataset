"""Tell the repo when the scheduled daily run is not ready, instead of leaving it red in the Actions tab.

    python3 pipeline/daily_issue.py --repo oxyc/den-dataset --run-url URL --ready true|false \
        --cancelled true|false --report out/daily-report.json --handoff "$RUNNER_TEMP/day-handoff"

The last step of .github/workflows/daily.yml runs this, whatever happened. One open issue labelled
`daily-failing` stands for "the daily run is failing": the first failed run opens it, each later one comments
and raises its count, and a ready run closes it. A CPU that failed the embed canary without being on
data/embed-cpu-blocklist.txt gets its own small issue (`embed-cpu-blocklist`), so adding it is a one-line PR.

The repo is public, so an issue carries only the verdict line, the run link, a count and a CPU model name:
never a log, and a verdict has URL queries and key=value secrets scrubbed before it is posted.

Title aliases the store held back for want of a keep/drop decision (store/aliases.py) are listed in one open
issue labelled `alias-review`, rewritten by each run that built a store and closed by one that held none. They
are Wikidata labels, which the published store carries anyway. Likewise one `data-review` issue lists what
the store's manifest records as waiting for a person without refusing the publish: titles several Wikidata
items claim with none chosen (`wikidataItems.ambiguous`) and stale award ceremony merges (`awardMerges.stale`).

pipeline/hand-off-day.sh writes the handoff file (key=value lines: state=handed|exhausted, why=, cpu=).
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys

LABEL = "daily-failing"
CPU_LABEL = "embed-cpu-blocklist"
ALIAS_LABEL = "alias-review"
REVIEW_LABEL = "data-review"
MARKER = "daily-consecutive-failures"
MAX_VERDICT = 400
MAX_ALIAS_ROWS = 400


def read_handoff(path):
    """The key=value lines hand-off-day.sh left, or {} when this run never handed anything on."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return {}
    return dict(line.split("=", 1) for line in lines if "=" in line)


def scrub(text):
    """One line, no markdown breakout, no URL query, no key=value secret, bounded."""
    text = " ".join(str(text).replace("`", "'").split())
    text = re.sub(r"(https?://[^\s?#]+)[?#]\S*", r"\1?…", text)
    text = re.sub(r"(?i)\b([\w-]*(?:key|token|secret|password|auth\w*|sig\w*))=\S+", r"\1=***", text)
    return text if len(text) <= MAX_VERDICT else text[:MAX_VERDICT - 1] + "…"


def read_report(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def outcome(ready, cancelled, report, handoff):
    """What this run was: ready, handed (the day moved to another attempt), exhausted, or failed."""
    state = handoff.get("state")
    cpu = scrub(handoff.get("cpu", "")) if handoff.get("cpu") else ""
    if state == "handed":
        return {"kind": "handed", "verdict": scrub(handoff.get("why", "")), "cpu": cpu}
    if state == "exhausted":
        why = scrub(handoff.get("why", ""))
        return {"kind": "exhausted", "cpu": cpu,
                "verdict": f"every handoff attempt is used and no run did the day's work; the day is skipped: {why}"}
    if ready:
        return {"kind": "ready", "verdict": "", "cpu": ""}
    if report.get("verdict"):
        verdict = scrub(report["verdict"])
    elif cancelled:
        verdict = "the run was cancelled or timed out before it finished"
    else:
        verdict = "the run failed before it wrote a report"
    return {"kind": "failed", "verdict": verdict, "cpu": ""}


def count_in(body):
    m = re.search(rf"<!-- {MARKER}: (\d+) -->", body or "")
    return int(m.group(1)) if m else 0


def issue_body(n, date, run_url, verdict, cpu):
    lines = ["The scheduled daily run is not ready to publish.", "",
             f"- Consecutive failed runs: {n}",
             f"- Latest: {date}, {run_url}",
             f"- Verdict: `daily: NOT ready — {verdict}`", ""]
    if cpu:
        lines += [f"Unlisted CPU `{cpu}` failed the embed canary: add that exact line to "
                  "data/embed-cpu-blocklist.txt (see the `embed-cpu-blocklist` issue).", ""]
    lines += ["A ready run closes this issue. docs/OPERATE.md, \"When a run is not ready\".", "",
              f"<!-- {MARKER}: {n} -->"]
    return "\n".join(lines)


def plan(out, issue, cpu_issue_titles, date, run_url):
    """The GitHub calls this run owes, as data. `issue` is the open daily-failing issue or None."""
    actions = []
    kind = out["kind"]
    if out["cpu"] and kind in ("handed", "exhausted"):
        title = f"Add {out['cpu']} to the embed CPU blocklist"
        if title not in cpu_issue_titles:
            body = (f"The embed canary failed on `{out['cpu']}` ({run_url}), a CPU that is not on "
                    "data/embed-cpu-blocklist.txt, so the day was handed to another runner.\n\n"
                    f"Add this line to data/embed-cpu-blocklist.txt:\n\n```\n{out['cpu']}\n```\n\n"
                    "docs/OPERATE.md, \"The alignment rule\".")
            actions.append({"do": "create", "label": CPU_LABEL, "title": title, "body": body})
    if kind in ("failed", "exhausted"):
        n = count_in(issue["body"]) + 1 if issue else 1
        body = issue_body(n, date, run_url, out["verdict"], out["cpu"])
        if issue:
            note = f"{date}, {run_url}: `daily: NOT ready — {out['verdict']}` (consecutive failed runs: {n})"
            actions.append({"do": "edit", "number": issue["number"], "body": body})
            actions.append({"do": "comment", "number": issue["number"], "body": note})
        else:
            actions.append({"do": "create", "label": LABEL, "title": f"The daily run is failing ({date})",
                            "body": body})
    elif kind == "ready" and issue:
        actions.append({"do": "close", "number": issue["number"],
                        "body": f"Ready again: {date}, {run_url}"})
    return actions


def read_alias_review(path):
    """The rows check_alias_collisions.py --review wrote, or None when this run wrote no review file."""
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            rows = json.load(f)
    except (OSError, ValueError):
        return None
    return rows if isinstance(rows, list) else None


def alias_body(rows, date, run_url):
    lines = [f"The store holds back {len(rows)} title alias(es) that are another title's name, until each has a "
             "keep or drop decision in data/alias-decisions.json. The titles ship; only these search names wait.",
             "", f"Latest: {date}, {run_url}", "",
             "Keep a real release, translated or working title; drop an alias that names a different work. "
             "`pipeline/check_alias_collisions.py <facts> --tmdb --review alias-review.json` sorts them by "
             "whether TMDB records the alias. A run that holds none closes this issue.", "",
             "| Title | Alias | Also the name of |", "|---|---|---|"]
    for r in rows[:MAX_ALIAS_ROWS]:
        cell = lambda s: scrub(s).replace("|", "\\|")
        owners = ", ".join(f"{o['mediaType']}:{o['tmdbId']}" for o in r.get("claimedBy") or [])
        lines.append(f"| {r['mediaType']}:{r['tmdbId']} {cell(r.get('title') or '')} | {cell(r['alias'])} "
                     f"| {owners} |")
    if len(rows) > MAX_ALIAS_ROWS:
        lines.append(f"\n… and {len(rows) - MAX_ALIAS_ROWS} more in the run's `alias-review.json` artifact.")
    return "\n".join(lines)


def plan_aliases(rows, issue, date, run_url):
    """The alias-review issue's calls. `rows` is None when no store was built, which says nothing."""
    if rows is None:
        return []
    if rows:
        body = alias_body(rows, date, run_url)
        if issue:
            return [{"do": "edit", "number": issue["number"], "body": body}]
        return [{"do": "create", "label": ALIAS_LABEL, "body": body,
                 "title": "Title aliases waiting for a keep/drop decision"}]
    if issue:
        return [{"do": "close", "number": issue["number"],
                 "body": f"Every colliding alias has a decision: {date}, {run_url}"}]
    return []


def read_review_items(meta_path):
    """`(ambiguous, stale)` from the store's manifest, or None when this run stamped no store."""
    try:
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError, TypeError):
        return None
    items, merges = meta.get("wikidataItems"), meta.get("awardMerges")
    if not (isinstance(items, dict) and isinstance(merges, dict)):
        return None
    return [scrub(k) for k in items.get("ambiguous") or []], [scrub(q) for q in merges.get("stale") or []]


def plan_review(found, issue, date, run_url):
    """The data-review issue's calls. `found` is None when no store was stamped, which says nothing."""
    if found is None:
        return []
    ambiguous, stale = found
    if not (ambiguous or stale):
        return [{"do": "close", "number": issue["number"],
                 "body": f"Nothing waits for a decision: {date}, {run_url}"}] if issue else []
    lines = ["The latest store published with these waiting for a person. Each is small; none stops a day.", "",
             f"Latest: {date}, {run_url}", ""]
    if ambiguous:
        lines += [f"**{len(ambiguous)} title(s) with no Wikidata item chosen** ship with no card. Several items "
                  "claim each one's TMDB id: pick one in data/wikidata-item-decisions.json "
                  "(`pipeline/check_wikidata_items.py` says how).", "", ", ".join(ambiguous), ""]
    if stale:
        lines += [f"**{len(stale)} stale award ceremony merge(s)** in data/award-ceremony-merges.json name a "
                  "ceremony no title does any more: delete each or point it at where the prizes file now.", "",
                  ", ".join(stale), ""]
    lines.append("A run that finds none closes this issue.")
    body = "\n".join(lines)
    if issue:
        return [{"do": "edit", "number": issue["number"], "body": body}]
    return [{"do": "create", "label": REVIEW_LABEL, "body": body,
             "title": "Dataset items waiting for a decision"}]


def gh(args, repo, stdin=None):
    done = subprocess.run(["gh", *args, "-R", repo], input=stdin, capture_output=True, text=True)
    if done.returncode:
        raise SystemExit(f"error: gh {' '.join(args[:2])} failed: {done.stderr.strip()[:300]}")
    return done.stdout


def apply(actions, repo):
    labelled = set()
    for a in actions:
        if a["do"] == "create":
            if a["label"] not in labelled:
                gh(["label", "create", a["label"], "--force", "--description",
                    "Opened by pipeline/daily_issue.py"], repo)
                labelled.add(a["label"])
            gh(["issue", "create", "--title", a["title"], "--label", a["label"], "--body-file", "-"],
               repo, a["body"])
        elif a["do"] == "edit":
            gh(["issue", "edit", str(a["number"]), "--body-file", "-"], repo, a["body"])
        elif a["do"] == "comment":
            gh(["issue", "comment", str(a["number"]), "--body-file", "-"], repo, a["body"])
        elif a["do"] == "close":
            gh(["issue", "close", str(a["number"]), "--comment", a["body"]], repo)


def open_issues(repo, label):
    return json.loads(gh(["issue", "list", "--label", label, "--state", "open", "--limit", "100",
                          "--json", "number,title,body"], repo) or "[]")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--run-url", required=True)
    ap.add_argument("--ready", choices=("true", "false"), required=True)
    ap.add_argument("--cancelled", choices=("true", "false"), default="false")
    ap.add_argument("--report", default="out/daily-report.json")
    ap.add_argument("--handoff", required=True)
    ap.add_argument("--alias-review", help="check_alias_collisions.py --review output, when a store was built")
    ap.add_argument("--meta", help="the dataset.meta.json the store build stamped, when it did")
    ap.add_argument("--dry-run", action="store_true", help="print the calls instead of making them")
    args = ap.parse_args(argv)

    report = read_report(args.report)
    out = outcome(args.ready == "true", args.cancelled == "true", report, read_handoff(args.handoff))
    date = report.get("date") or datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    review = read_alias_review(args.alias_review)
    if review is not None:
        alias_issues = open_issues(args.repo, ALIAS_LABEL)
        alias_actions = plan_aliases(review, alias_issues[0] if alias_issues else None, date, args.run_url)
        if args.dry_run:
            print(json.dumps(alias_actions, indent=2, ensure_ascii=False))
        else:
            apply(alias_actions, args.repo)
    found = read_review_items(args.meta) if args.meta else None
    if found is not None:
        review_issues = open_issues(args.repo, REVIEW_LABEL)
        review_actions = plan_review(found, review_issues[0] if review_issues else None, date, args.run_url)
        if args.dry_run:
            print(json.dumps(review_actions, indent=2, ensure_ascii=False))
        else:
            apply(review_actions, args.repo)
    if out["kind"] == "handed" and not out["cpu"]:
        print("daily_issue: handed to another attempt; nothing to report")
        return 0
    issues = open_issues(args.repo, LABEL) if out["kind"] != "handed" else []
    cpu_titles = {i["title"] for i in open_issues(args.repo, CPU_LABEL)} if out["cpu"] else set()
    actions = plan(out, issues[0] if issues else None, cpu_titles, date, args.run_url)
    if args.dry_run:
        print(json.dumps(actions, indent=2, ensure_ascii=False))
        return 0
    apply(actions, args.repo)
    print(f"daily_issue: {out['kind']}; {len(actions)} call(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
