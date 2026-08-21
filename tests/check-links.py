#!/usr/bin/env python3
"""Check that every relative markdown link resolves.

Files come in as arguments, not on stdin. That is the whole point of this file
existing: this check used to be a python heredoc inside the workflow, written as

    git ls-files '*.md' | python - <<'PY' ... for line in sys.stdin: ...

where the heredoc IS stdin, so the interpreter read the program from it and the
piped file list went nowhere. The loop ran zero times, `bad` stayed empty, and
the job reported success for every push since it was written - a deliberately
broken link exits 0. Python embedded in a shell heredoc is how that happens, so
it lives in a file now.

Absolute links (http, mailto, #anchor) are not checked; a link checker that
reaches the network is a different job with different failure modes.
"""
import os
import re
import sys

LINK = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
EXTERNAL = re.compile(r"^(https?:|mailto:|#)")


def broken(paths):
    """-> (list of broken links, number of relative links looked at)."""
    out, seen = [], 0
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        for label, target in LINK.findall(text):
            if EXTERNAL.match(target):
                continue
            #  Split off an anchor; nothing here verifies headings.
            rel = target.split("#")[0]
            if not rel:                              # a bare "#anchor"
                continue
            seen += 1
            if not os.path.exists(os.path.join(os.path.dirname(path), rel)):
                out.append("%s: [%s](%s)" % (path, label, target))
    return out, seen


if __name__ == "__main__":
    files = sys.argv[1:]
    if not files:
        sys.stderr.write("usage: check-links.py <file.md> ...\n")
        raise SystemExit(2)
    bad, seen = broken(files)
    for b in bad:
        print("::error::broken link %s" % b)
    print("%d relative links in %d files, %d broken" % (seen, len(files), len(bad)))
    raise SystemExit(1 if bad else 0)
