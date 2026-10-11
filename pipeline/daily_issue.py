"""Tell the repo when the scheduled daily run is not ready, instead of leaving it red in the Actions tab.

    python3 pipeline/daily_issue.py --repo oxyc/den-dataset --run-url URL --ready true|false \
        --cancelled true|false --report out/daily-report.json --handoff "$RUNNER_TEMP/day-handoff"

The last step of .github/workflows/daily.yml runs this, whatever happened. One open issue labelled
`daily-failing` stands for "the daily run is failing": the first failed run opens it, each later one comments
and raises its count, and a ready run closes it. A CPU that failed the embed canary without being on
data/embed-cpu-blocklist.txt gets its own small issue (`embed-cpu-blocklist`), so adding it is a one-line PR.

The repo is public, so an issue carries only the verdict line, the run link, a count and a CPU model name:
never a log, and a verdict has URL queries and key=value secrets scrubbed before it is posted.

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
MARKER = "daily-consecutive-failures"
MAX_VERDICT = 400


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
    ap.add_argument("--dry-run", action="store_true", help="print the calls instead of making them")
    args = ap.parse_args(argv)

    report = read_report(args.report)
    out = outcome(args.ready == "true", args.cancelled == "true", report, read_handoff(args.handoff))
    date = report.get("date") or datetime.datetime.now(datetime.timezone.utc).date().isoformat()
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
