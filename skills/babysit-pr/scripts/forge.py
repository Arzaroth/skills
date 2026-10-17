"""Forge access for the babysit-pr skill: GitHub (gh) and GitLab (glab).

The one place that knows a forge's API shapes. `gh`/`glab` are used as transport
and auth only; every request is addressed explicitly, so nothing depends on a
CLI's own idea of which repo the working directory means.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from classify import SPENDS_MONEY, is_bot, is_bot_command, reviewer_status

CREDS_PATH = os.environ.get(
    "BABYSIT_CREDENTIALS", os.path.expanduser("~/.claude/babysit-pr/credentials")
)

# Rotation state and trigger budgets. Not git config: that follows the working
# directory rather than the repo being addressed.
STATE_DIR = pathlib.Path(
    os.environ.get("BABYSIT_STATE", "~/.claude/babysit-pr/state")).expanduser()

# CodeRabbit's quota is hourly. Used only when the notice does not say.
ASSUMED_QUOTA_WINDOW = 3600

DEFAULT_IDENTITY = "default"

# A named credential the forge refuses outright (expired or revoked token). Parked
# like a spent quota so rotation skips it, but for long enough that nobody waits it
# out: a dead token does not come back on its own.
DEAD_CREDENTIAL_WINDOW = 7 * 24 * 3600
AUTH_REJECTED = re.compile(r"HTTP 401|invalid_token|Token is expired|Bad credentials", re.I)


class ForgeError(RuntimeError):
    pass


class PaidActionRefused(ForgeError):
    """Raised instead of posting something that bills the user."""


class AddressError(ForgeError):
    """Raised when a PR number has no repo to resolve it against."""


def run(argv, *, stdin=None, env=None):
    p = subprocess.run(
        argv, input=stdin, capture_output=True, text=True,
        env={**os.environ, **(env or {})},
    )
    if p.returncode != 0:
        detail = (p.stderr.strip() + " " + p.stdout.strip()).strip()
        raise ForgeError(f"{argv[0]} failed ({p.returncode}): {detail[:600]}")
    return p.stdout


def git(*args, default=""):
    p = subprocess.run(["git", *args], capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else default


def json_stream(text):
    """Parse one or more concatenated JSON documents (what --paginate emits)."""
    dec, out, i = json.JSONDecoder(), [], 0
    while i < len(text):
        while i < len(text) and text[i] in " \n\r\t":
            i += 1
        if i >= len(text):
            break
        obj, i = dec.raw_decode(text, i)
        out.extend(obj if isinstance(obj, list) else [obj])
    return out


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
def parse_remote(url):
    u = url.strip()
    if u.startswith("ssh://"):
        rest = u[6:].split("@", 1)[-1]
        host, _, path = rest.partition("/")
        host = host.split(":", 1)[0]
    elif u.startswith(("http://", "https://")):
        rest = u.split("://", 1)[1].split("@", 1)[-1]
        host, _, path = rest.partition("/")
    elif "@" in u and ":" in u:
        host, _, path = u.split("@", 1)[1].partition(":")
    else:
        raise ForgeError(f"unsupported remote url: {url}")
    if not host or not path:
        raise ForgeError(f"unsupported remote url: {url}")
    return host, path[:-4] if path.endswith(".git") else path


def _probe(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def detect_kind(host):
    if os.environ.get("BABYSIT_FORGE"):
        return os.environ["BABYSIT_FORGE"]
    # Keyed by host: an agent standing in a GitHub checkout can address a GitLab
    # repo, and an unkeyed cache would hand it the wrong API.
    cache_key = "babysit.forge." + re.sub(r"[^A-Za-z0-9]", "-", host)
    cached = git("config", "--get", cache_key)
    if cached:
        return cached
    if host == "github.com" or host.endswith(".github.com"):
        kind = "github"
    elif host == "gitlab.com" or host.endswith(".gitlab.com"):
        kind = "gitlab"
    # Self-hosted: GitLab answers /api/v4/version even unauthenticated (401);
    # GitHub Enterprise serves /api/v3 metadata.
    elif _probe(f"https://{host}/api/v4/version") in (200, 401, 403):
        kind = "gitlab"
    elif _probe(f"https://{host}/api/v3/") in (200, 401, 403):
        kind = "github"
    else:
        raise ForgeError(
            f"cannot determine forge for {host}; set BABYSIT_FORGE=github|gitlab"
        )
    git("config", cache_key, kind)
    return kind


def ambient_target():
    """What the working directory says, which is a guess about intent."""
    remote = os.environ.get("BABYSIT_REMOTE", "origin")
    url = git("remote", "get-url", remote)
    return parse_remote(url) if url else (None, None)


def parse_target(repo):
    """An address: a remote url, `host/owner/name`, or `owner/name`.

    A bare path borrows the host from the working directory, so it only
    addresses the forge the caller already stands on."""
    if "://" in repo or re.search(r"@[^/]+:", repo):
        return parse_remote(repo)
    parts = repo.strip("/").split("/")
    # `git.example.com/group/repo`. A top-level group with a dot in its name
    # collides with this; pass a full url for that one.
    if len(parts) >= 3 and ("." in parts[0] or parts[0] == "localhost"):
        return parts[0], "/".join(parts[1:])
    if len(parts) < 2:
        raise AddressError(
            f"'{repo}' is not a repo address: want owner/name, host/owner/name, "
            "or a remote url")
    host, _ = ambient_target()
    if not host:
        raise AddressError(
            f"cannot resolve a host for '{repo}': not in a git repo, or no "
            "remote. Pass host/owner/name, or a full remote url.")
    return host, repo.strip("/")


def detect(repo=None):
    repo = repo or os.environ.get("BABYSIT_REPO")
    if repo:
        host, path = parse_target(repo)
    else:
        remote = os.environ.get("BABYSIT_REMOTE", "origin")
        host, path = ambient_target()
        if not host:
            raise AddressError(
                f"no repo given, and no '{remote}' remote here to borrow one "
                "from. Pass host/owner/name or a remote url.")
    cls = {"github": GitHubForge, "gitlab": GitLabForge}[detect_kind(host)]
    return cls(host, path)


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
def _slot(host, name):
    return re.sub(r"[^A-Za-z0-9._-]", "-", f"{host}#{name}")


def limited_until(host, name):
    """Epoch second this identity's review quota is expected back, 0 if free."""
    try:
        until = float((STATE_DIR / f"limited-{_slot(host, name)}").read_text().strip())
    except (OSError, ValueError):
        return 0
    return until if until > time.time() else 0


def mark_limited(host, name, until=None):
    """Record that this identity is out of review quota.

    Kept per host and identity rather than per PR: the quota belongs to the
    account, so a limit hit on one PR is still in force on the next."""
    path = STATE_DIR / f"limited-{_slot(host, name)}"
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(str(float(until or time.time() + ASSUMED_QUOTA_WINDOW)))
    except OSError:
        pass


def load_creds(host):
    if not os.path.exists(CREDS_PATH):
        return []
    mode = oct(os.stat(CREDS_PATH).st_mode & 0o777)[2:]
    if mode not in ("600", "400"):
        print(f"forge: warning: {CREDS_PATH} is mode {mode}, want 600", file=os.sys.stderr)
    out = []
    with open(CREDS_PATH) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) >= 3 and parts[1] in (host, "*"):
                out.append((parts[0], parts[2]))
    return out


# ---------------------------------------------------------------------------
# Forges
# ---------------------------------------------------------------------------
def _source(login):
    return "bot" if is_bot(login) else "human"


# CodeRabbit writes its walkthrough INTO the PR description, under this heading.
# Rewriting the body without carrying it over deletes the reviewer's own work,
# and the bot does not put it back until it reviews again.
BOT_SECTIONS = (
    re.compile(r"<!--\s*This is an auto-generated comment: summarize by coderabbit\.ai\s*-->.*?"
               r"<!--\s*end of auto-generated comment.*?-->", re.S | re.I),
    re.compile(r"(?:^|\n)#{1,4}\s*Summary by CodeRabbit.*?(?=\n#{1,4}\s|\Z)", re.S | re.I),
)


def bot_sections(body):
    """Blocks in a PR description that a reviewer owns, not the author."""
    out = []
    for rx in BOT_SECTIONS:
        out += [m.group(0).strip() for m in rx.finditer(body or "")]
    return out


def post_guard(body, allow_paid=False):
    """Refuse to post anything that spends the user's money.

    A rate-limited CodeRabbit suggests `@coderabbitai review --use-credits`,
    which bills. An agent reading that suggestion and acting on it is exactly the
    decision a user must make for themselves, so the refusal lives here, at the
    write boundary, rather than in a prompt any caller might not have read.

    `allow_paid` exists only for a spend the user has explicitly authorised.
    Never set it from a bot's own suggestion."""
    if allow_paid:
        return body
    m = SPENDS_MONEY.search(body or "")
    if m:
        raise PaidActionRefused(
            f"refusing to post {m.group(0)!r}: it spends credits and costs money. "
            "Ask the user first; if they authorise it, pass allow_paid/--allow-paid.")
    return body


def _set_body(forge, pr, text, keep_bot_sections, write):
    """Replace the description, carrying the reviewer's own sections across.

    Dropping them is silent and unrecoverable: the bot rewrites its summary only
    on its next review, so a clobbered description stays clobbered while everyone
    waits for it to come back on its own."""
    kept = []
    if keep_bot_sections:
        kept = [s for s in bot_sections(forge.body(pr)) if s not in (text or "")]
        if kept:
            text = (text or "").rstrip() + "\n\n" + "\n\n".join(kept) + "\n"
    write(text)
    return len(kept)


class Forge:
    kind = cli = term = "?"
    TOKEN_VARS = ()

    def _clear_token(self):
        # Everything _set_token put there, not just the token: a GITLAB_HOST left
        # behind points glab at a host the user's own login does not cover, and
        # every call then answers "404 Project Not Found".
        self.env = {}

    def __init__(self, host, path):
        self.host, self.path = host, path
        self.env = {}
        self.identity = DEFAULT_IDENTITY

    # -- credentials -------------------------------------------------------
    def identities(self):
        """Slot 0 is the user's own default auth, so no credentials file degrades
        to normal behaviour instead of failing."""
        return [DEFAULT_IDENTITY] + [n for n, _ in load_creds(self.host)]

    def _run(self, argv, *, stdin=None):
        """Run a CLI call as the current identity, and survive a dead credential.

        Rotation swaps the token for EVERY call, reads included. Measured on a live
        MR: the watcher rotated onto an account whose token had expired, every poll
        then failed with 401, and it sat blind for 25 minutes on the last verdict it
        had read while the review finished and the MR went mergeable."""
        try:
            return run(argv, stdin=stdin, env=self.env)
        except ForgeError as exc:
            if self.identity == DEFAULT_IDENTITY or not AUTH_REJECTED.search(str(exc)):
                raise
            dead = self.identity
            mark_limited(self.host, dead, time.time() + DEAD_CREDENTIAL_WINDOW)
            self.use_credential(DEFAULT_IDENTITY)
            print(f"forge: credential '{dead}' was refused ({str(exc)[:120]}); "
                  f"parked, continuing as '{DEFAULT_IDENTITY}'", file=os.sys.stderr)
            return run(argv, stdin=stdin, env=self.env)

    def use_credential(self, name):
        if name == DEFAULT_IDENTITY:
            self._clear_token()
            self.identity = DEFAULT_IDENTITY
            return name
        creds = dict(load_creds(self.host))
        if name not in creds:
            raise ForgeError(f"no credential '{name}' for host {self.host} in {CREDS_PATH}")
        self._set_token(creds[name])
        self.identity = name
        return name

    def fresh_credential(self):
        """An identity with review quota left, other than the current one.

        None means every account is rate limited, which is a reason to wait
        rather than to keep posting."""
        return next((n for n in self.identities()
                     if n != self.identity and not limited_until(self.host, n)), None)

    def avoid_limited(self):
        """Step off an identity already known to be out of quota.

        Spending the first trigger of a run on an account that answered "review
        limit reached" ten minutes ago buys nothing."""
        if not limited_until(self.host, self.identity):
            return None
        fresh = self.fresh_credential()
        return self.use_credential(fresh) if fresh else None

    def rotate_credential(self):
        """Advance to the next identity that still has quota."""
        names = self.identities()
        if len(names) == 1:
            return DEFAULT_IDENTITY
        key = STATE_DIR / f"rotate-{_slot(self.host, 'idx')}"
        try:
            idx = int(key.read_text().strip())
        except (OSError, ValueError):
            idx = 0
        for step in range(1, len(names) + 1):
            nxt = (idx + step) % len(names)
            if limited_until(self.host, names[nxt]):
                continue
            try:
                STATE_DIR.mkdir(parents=True, exist_ok=True)
                key.write_text(str(nxt))
            except OSError:
                pass
            return self.use_credential(names[nxt])
        return self.identity

    def authenticated(self):
        try:
            run([self.cli, "auth", "status"], env=self.env)
            return True
        except ForgeError:
            return False

    def reviewer_status(self, sha):
        """What the reviewer's commit status says about this head right now."""
        return reviewer_status(self.commit_statuses(sha))

    @property
    def slug(self):
        return f"{self.host}/{self.path}"

    def describe(self):
        return {"forge": self.kind, "slug": self.slug,
                "host": self.host, "path": self.path,
                "cli": self.cli, "term": self.term,
                "auth": "ok" if self.authenticated() else "missing"}


class GitHubForge(Forge):
    kind, cli, term = "github", "gh", "pull request"

    THREADS_QUERY = """query($o:String!,$r:String!,$n:Int!){
      repository(owner:$o,name:$r){ pullRequest(number:$n){
        number url state isDraft merged mergeable mergeStateStatus
        headRefName baseRefName headRefOid
        commits(last:1){nodes{commit{statusCheckRollup{state}}}}
        reviewThreads(first:100){nodes{
          id isResolved isOutdated path line originalLine
          comments(first:30){nodes{author{login} body url createdAt}}}}
      }}}"""

    TOKEN_VARS = ("GH_TOKEN", "GITHUB_TOKEN")

    def _set_token(self, tok):
        self.env.update(GH_TOKEN=tok, GITHUB_TOKEN=tok)

    def _api(self, path, *, method=None, body=None, paginate=False):
        argv = ["gh", "api", "--hostname", self.host]
        if method:
            argv += ["--method", method]
        if paginate:
            argv += ["--paginate"]
        if body is not None:
            argv += ["--input", "-"]
        argv.append(path)
        out = self._run(argv, stdin=json.dumps(body) if body is not None else None)
        return json_stream(out) if paginate else (json.loads(out) if out.strip() else {})

    def _graphql(self, query, **vars):
        argv = ["gh", "api", "--hostname", self.host, "graphql", "-f", f"query={query}"]
        for k, v in vars.items():
            argv += ["-F", f"{k}={v}"]
        return json.loads(self._run(argv))["data"]

    def _pr(self, pr):
        o, r = self.path.split("/", 1)
        return self._graphql(self.THREADS_QUERY, o=o, r=r, n=pr)["repository"]["pullRequest"]

    def state(self, pr):
        p = self._pr(pr)
        nodes = p["commits"]["nodes"]
        ci = (nodes[0]["commit"].get("statusCheckRollup") or {}).get("state") if nodes else None
        threads = p["reviewThreads"]["nodes"]
        return {
            "number": p["number"], "url": p["url"], "head": p["headRefOid"],
            "head_branch": p["headRefName"], "base": p["baseRefName"],
            "state": "merged" if p["merged"] else p["state"].lower(),
            "draft": str(p["isDraft"]).lower(),
            "ci": (ci or "none").lower(),
            # GitHub says MERGEABLE/CONFLICTING/UNKNOWN, GitLab says true/conflict.
            # Callers compare one vocabulary, so normalise here or a conflicted
            # GitHub PR never matches the conflict check.
            "mergeable": {"mergeable": "true", "conflicting": "conflict"}.get(
                str(p.get("mergeable") or "unknown").lower(), "unknown"),
            "merge_state": str(p.get("mergeStateStatus") or "unknown").lower(),
            "threads_total": len(threads),
            "unresolved": sum(1 for t in threads if not t["isResolved"]),
        }

    def threads(self, pr, unresolved_only=False):
        out = []
        for t in self._pr(pr)["reviewThreads"]["nodes"]:
            if unresolved_only and t["isResolved"]:
                continue
            cs = t["comments"]["nodes"]
            first = cs[0] if cs else {}
            author = ((first.get("author") or {}).get("login")) or "?"
            out.append({
                "id": t["id"], "author": author, "source": _source(author),
                "file": t.get("path"), "line": t.get("line") or t.get("originalLine"),
                "resolved": t["isResolved"], "outdated": t["isOutdated"],
                "url": first.get("url"), "replies": max(0, len(cs) - 1),
                "body": first.get("body", ""),
                "thread": [{"author": (c.get("author") or {}).get("login"),
                            "body": c.get("body", "")} for c in cs[1:]],
            })
        return out

    def reviews(self, pr):
        out = []
        for ep, ts in ((f"repos/{self.path}/pulls/{pr}/reviews", "submitted_at"),
                       (f"repos/{self.path}/issues/{pr}/comments", "created_at")):
            for n in self._api(ep + "?per_page=100", paginate=True):
                if (n.get("body") or "").strip():
                    out.append({"id": str(n.get("id")),
                                "author": (n.get("user") or {}).get("login", ""),
                                "created_at": n.get(ts) or n.get("created_at"),
                                "updated_at": n.get("updated_at"),
                                "body": n["body"]})
        return sorted(out, key=lambda c: c["created_at"] or "")

    def approvals(self, pr):
        return [{"author": (n.get("user") or {}).get("login", ""),
                 "state": n["state"].lower(), "at": n.get("submitted_at"),
                 "body": n.get("body") or ""}
                for n in self._api(f"repos/{self.path}/pulls/{pr}/reviews?per_page=100",
                                   paginate=True)
                if n.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED")]

    def find(self, branch):
        owner = self.path.split("/", 1)[0]
        got = self._api(f"repos/{self.path}/pulls?state=open&head={owner}:{branch}")
        return got[0]["number"] if got else None

    def create(self, head, base, title, body, draft=False):
        return self._api(f"repos/{self.path}/pulls", method="POST", body={
            "title": title, "head": head, "base": base, "body": body, "draft": draft,
        })["number"]

    def reply(self, pr, thread_id, body, allow_paid=False):
        self._graphql(
            "mutation($t:ID!,$b:String!){addPullRequestReviewThreadReply"
            "(input:{pullRequestReviewThreadId:$t,body:$b}){comment{url}}}",
            t=thread_id, b=post_guard(body, allow_paid))

    def resolve(self, pr, thread_id):
        self._graphql(
            "mutation($t:ID!){resolveReviewThread(input:{threadId:$t})"
            "{thread{isResolved}}}", t=thread_id)

    def commit_statuses(self, sha):
        return [{"name": st.get("context"), "state": st.get("state"),
                 "description": st.get("description"),
                 "at": st.get("updated_at") or st.get("created_at")}
                for st in self._api(f"repos/{self.path}/commits/{sha}/statuses?per_page=100")]

    def comment(self, pr, body, allow_paid=False):
        """Returns the new comment's id so a caller can recognise its own post."""
        got = self._api(f"repos/{self.path}/issues/{pr}/comments", method="POST",
                        body={"body": post_guard(body, allow_paid)})
        return str(got.get("id")) if got.get("id") is not None else None

    def review_comment(self, pr, path, line, body, allow_paid=False):
        self._api(f"repos/{self.path}/pulls/{pr}/comments", method="POST", body={
            "commit_id": self.state(pr)["head"], "path": path,
            "line": int(line), "side": "RIGHT", "body": post_guard(body, allow_paid),
        })

    def body(self, pr):
        return self._api(f"repos/{self.path}/pulls/{pr}").get("body") or ""

    def set_body(self, pr, text, keep_bot_sections=True):
        return _set_body(self, pr, text, keep_bot_sections, lambda new: self._api(
            f"repos/{self.path}/pulls/{pr}", method="PATCH", body={"body": new}))

    def rebase(self, pr):
        self._api(f"repos/{self.path}/pulls/{pr}/update-branch", method="PUT")
        return "update requested"

    def refresh_diff(self, pr):
        return "not needed on github"

    def merge(self, pr, squash=False, keep_branch=False):
        branch = self.state(pr)["head_branch"]
        got = self._api(f"repos/{self.path}/pulls/{pr}/merge", method="PUT",
                        body={"merge_method": "squash" if squash else "merge"})
        if not got.get("merged"):
            raise ForgeError(f"merge refused: {got.get('message') or got}")
        if not keep_branch:
            try:
                self._api(f"repos/{self.path}/git/refs/heads/{branch}", method="DELETE")
            except ForgeError:
                pass
        return (got.get("sha") or "")[:9]


class GitLabForge(Forge):
    kind, cli, term = "gitlab", "glab", "merge request"

    TOKEN_VARS = ("GITLAB_TOKEN",)

    def _set_token(self, tok):
        self.env.update(GITLAB_TOKEN=tok, GITLAB_HOST=self.host)

    @property
    def proj(self):
        return "projects/" + urllib.parse.quote(self.path, safe="")

    def _api(self, path, *, method=None, fields=None, paginate=False):
        argv = ["glab", "api"]
        if method:
            argv += ["--method", method]
        if paginate:
            argv += ["--paginate"]
        for k, v in (fields or {}).items():
            argv += ["-f", f"{k}={v}"]
        argv.append(f"{self.proj}/{path}")
        out = self._run(argv)
        return json_stream(out) if paginate else (json.loads(out) if out.strip() else {})

    @staticmethod
    def _resolvable(notes):
        return [n for n in notes if not n.get("system")]

    @staticmethod
    def _is_command_thread(notes):
        """A discussion opened purely to talk to the reviewer is not feedback.

        GitLab makes every top-level note a resolvable discussion, so our own
        `@coderabbitai review` counts toward `unresolved` and blocks the merge
        precondition that requires it to be zero."""
        if not notes:
            return False
        first = notes[0]
        return (not (first.get("position") or {})
                and is_bot_command(first.get("body")))

    def _discussions(self, pr):
        return self._api(f"merge_requests/{pr}/discussions?per_page=100", paginate=True)

    def state(self, pr):
        d = self._api(f"merge_requests/{pr}?include_rebase_in_progress=true")
        total = unresolved = 0
        for disc in self._discussions(pr):
            notes = self._resolvable(disc.get("notes") or [])
            if not any(n.get("resolvable") for n in notes):
                continue
            if self._is_command_thread(notes):
                continue
            total += 1
            if not all(n.get("resolved") for n in notes if n.get("resolvable")):
                unresolved += 1
        return {
            "number": d["iid"], "url": d.get("web_url"), "head": d.get("sha") or "",
            "head_branch": d.get("source_branch"), "base": d.get("target_branch"),
            # normalise to the github vocabulary so callers compare one set of words
            "state": {"opened": "open", "locked": "open"}.get(d.get("state"), d.get("state")),
            "draft": str(bool(d.get("draft"))).lower(),
            "ci": (d.get("head_pipeline") or {}).get("status") or "none",
            "mergeable": ("conflict" if d.get("has_conflicts")
                          else str(d.get("detailed_merge_status") == "mergeable").lower()),
            "merge_state": d.get("detailed_merge_status") or "unknown",
            "rebase_in_progress": str(bool(d.get("rebase_in_progress"))).lower(),
            "merge_error": d.get("merge_error") or "",
            "blocking_discussions_resolved": str(bool(d.get("blocking_discussions_resolved"))).lower(),
            "changes": d.get("changes_count") or "0",
            "threads_total": total, "unresolved": unresolved,
        }

    def threads(self, pr, unresolved_only=False):
        out = []
        for disc in self._discussions(pr):
            notes = self._resolvable(disc.get("notes") or [])
            if not notes or not any(n.get("resolvable") for n in notes):
                continue
            if self._is_command_thread(notes):
                continue
            resolved = all(n.get("resolved") for n in notes if n.get("resolvable"))
            if unresolved_only and resolved:
                continue
            first = notes[0]
            pos = first.get("position") or {}
            author = (first.get("author") or {}).get("username", "")
            out.append({
                "id": disc["id"], "author": author, "source": _source(author),
                "file": pos.get("new_path") or pos.get("old_path"),
                "line": pos.get("new_line") or pos.get("old_line"),
                "resolved": resolved, "outdated": not pos.get("new_line"),
                "url": None, "replies": max(0, len(notes) - 1),
                "body": first.get("body", ""),
                "thread": [{"author": (n.get("author") or {}).get("username"),
                            "body": n.get("body", "")} for n in notes[1:]],
            })
        return out

    def reviews(self, pr):
        notes = self._api(
            f"merge_requests/{pr}/notes?per_page=100&sort=asc&order_by=created_at",
            paginate=True)
        return [{"id": str(n.get("id")),
                 "author": (n.get("author") or {}).get("username", ""),
                 "created_at": n.get("created_at"),
                 "updated_at": n.get("updated_at"),
                 "body": n["body"]}
                for n in notes if not n.get("system") and (n.get("body") or "").strip()]

    def approvals(self, pr):
        try:
            d = self._api(f"merge_requests/{pr}/approvals")
        except ForgeError:
            return []
        return [{"author": (a.get("user") or {}).get("username", ""),
                 "state": "approved", "at": None, "body": ""}
                for a in (d.get("approved_by") or [])]

    def find(self, branch):
        enc = urllib.parse.quote(branch, safe="")
        got = self._api(f"merge_requests?state=opened&source_branch={enc}&per_page=5")
        return got[0]["iid"] if got else None

    def create(self, head, base, title, body, draft=False):
        return self._api("merge_requests", method="POST", fields={
            "source_branch": head, "target_branch": base,
            "title": ("Draft: " if draft else "") + title,
            "description": body, "remove_source_branch": "true",
        })["iid"]

    def reply(self, pr, thread_id, body, allow_paid=False):
        self._api(f"merge_requests/{pr}/discussions/{thread_id}/notes",
                  method="POST", fields={"body": post_guard(body, allow_paid)})

    def resolve(self, pr, thread_id):
        self._api(f"merge_requests/{pr}/discussions/{thread_id}",
                  method="PUT", fields={"resolved": "true"})

    def commit_statuses(self, sha):
        # A status that goes running -> success is one record whose timestamps
        # move, so the newest of started/finished/created is when it was set.
        return [{"name": st.get("name"), "state": st.get("status"),
                 "description": st.get("description"),
                 "at": max(t for t in (st.get("finished_at"), st.get("started_at"),
                                       st.get("created_at"), "") if t is not None)}
                for st in self._api(f"repository/commits/{sha}/statuses?all=true")]

    def comment(self, pr, body, allow_paid=False):
        """Returns the new note's id so a caller can recognise its own post."""
        got = self._api(f"merge_requests/{pr}/notes", method="POST",
                        fields={"body": post_guard(body, allow_paid)})
        return str(got.get("id")) if got.get("id") is not None else None

    def review_comment(self, pr, path, line, body, allow_paid=False):
        refs = self._api(f"merge_requests/{pr}").get("diff_refs") or {}
        if not refs.get("head_sha"):
            raise ForgeError(f"MR {pr} has no diff_refs (stale diff? try refresh-diff)")
        self._api(f"merge_requests/{pr}/discussions", method="POST", fields={
            "body": post_guard(body, allow_paid),
            "position[position_type]": "text",
            "position[base_sha]": refs["base_sha"],
            "position[start_sha]": refs["start_sha"],
            "position[head_sha]": refs["head_sha"],
            "position[new_path]": path, "position[old_path]": path,
            "position[new_line]": line,
        })

    def body(self, pr):
        return self._api(f"merge_requests/{pr}").get("description") or ""

    def set_body(self, pr, text, keep_bot_sections=True):
        return _set_body(self, pr, text, keep_bot_sections, lambda new: self._api(
            f"merge_requests/{pr}", method="PUT", fields={"description": new}))

    def rebase(self, pr):
        self._api(f"merge_requests/{pr}/rebase", method="PUT")
        return "rebase requested"

    def refresh_diff(self, pr):
        """GitLab keeps stale diff refs after a retarget or force-push; a
        close/reopen cycle is the only reliable way to make it recompute."""
        import time
        self._api(f"merge_requests/{pr}?state_event=close", method="PUT")
        time.sleep(3)
        self._api(f"merge_requests/{pr}?state_event=reopen", method="PUT")
        time.sleep(6)
        return "diff recomputed"

    def merge(self, pr, squash=False, keep_branch=False):
        q = (f"squash={'true' if squash else 'false'}"
             f"&should_remove_source_branch={'false' if keep_branch else 'true'}")
        got = self._api(f"merge_requests/{pr}/merge?{q}", method="PUT")
        if got.get("state") != "merged":
            raise ForgeError(
                f"merge refused: {got.get('state')} "
                f"{got.get('message') or got.get('merge_error') or ''}".strip())
        return (got.get("merge_commit_sha") or got.get("squash_commit_sha") or "")[:9]
