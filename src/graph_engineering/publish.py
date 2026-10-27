"""The privileged zone: pushing, opening pull requests and merging. Only harness code runs here.

GitHub publication goes through `gh` with the person's keyring credentials, which the sandbox cannot read.
A merge needs every condition at one head commit: the person's approval, passing checks, satisfied reviews,
a mergeable state and readable branch protection. Admin rights are never used.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import quote

from .sandbox import GIT, SAFE_GIT, git

GH = shutil.which("gh") or "gh"   # resolved once, at import
SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
STATUS_CONTEXT = "graph-engineering/harness"


class PublishError(RuntimeError):
    pass


class APIError(PublishError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def marker(key: str) -> str:
    return f"<!-- graph-engineering:{key} -->"


def head_sha(value: str) -> str:
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise PublishError("invalid commit id")
    return value


def merge_gate(snapshot: dict, approved_head: str | None, required_checks: list[str] = ()) -> list[str]:
    """Every reason not to merge; missing evidence is always a reason."""
    reasons = []
    head = snapshot.get("head")
    if not head or not SHA.fullmatch(head):
        reasons.append("invalid PR head")
    if not head or approved_head != head:
        reasons.append("approval missing or given for another commit")
    for key, message in (("checks_present", "no checks reported"), ("checks_passed", "checks failed or still running"),
                         ("reviews_satisfied", "required reviews not satisfied"),
                         ("mergeable", "PR is conflicting, behind or its mergeability is unknown"),
                         ("policy_known", "branch protection could not be read")):
        if snapshot.get(key) is not True:
            reasons.append(message)
    if snapshot.get("draft") is not False:
        reasons.append("PR is a draft")
    missing = set(required_checks) - set(snapshot.get("check_names", []))
    if missing:
        reasons.append("required checks not reported: " + ", ".join(sorted(missing)))
    return reasons


def pending_only(reasons: list[str]) -> bool:
    """Reasons that resolve by waiting (CI running, mergeability computing)."""
    waiting = {"checks failed or still running", "no checks reported", "PR is conflicting, behind or its mergeability is unknown"}
    return bool(reasons) and all(r in waiting or r.startswith("required checks not reported") for r in reasons)


class GitHub:
    kind = "github"

    def __init__(self, slug: str, base: str):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", slug or ""):
            raise PublishError("invalid GitHub repository slug")
        self.slug, self.base, self.prefix = slug, base, f"repos/{slug}"

    def api(self, endpoint: str, method: str = "GET", payload: dict | None = None):
        command = [GH, "api", "--hostname", "github.com", "--method", method, "-H", "Accept: application/vnd.github+json", endpoint]
        if payload is not None:
            command += ["--input", "-"]
        try:
            result = subprocess.run(command, input=json.dumps(payload) if payload is not None else None, text=True,
                                    capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PublishError(f"GitHub API unavailable: {type(exc).__name__}") from exc
        if result.returncode:
            match = re.search(r"HTTP (\d{3})", result.stderr)
            raise APIError(f"GitHub API {method} {endpoint.split('?')[0]} failed: {result.stderr.strip()[:300]}",
                           int(match.group(1)) if match else None)
        try:
            return json.loads(result.stdout) if result.stdout.strip() else {}
        except ValueError as exc:
            raise PublishError("GitHub returned invalid JSON") from exc

    def pages(self, endpoint: str, field: str | None = None, limit: int = 20) -> list:
        items = []
        for page in range(1, limit + 1):
            data = self.api(f"{endpoint}{'&' if '?' in endpoint else '?'}per_page=100&page={page}")
            batch = data.get(field, []) if field else data
            items.extend(batch)
            if len(batch) < 100:
                break
        return items

    def ensure_issue(self, key: str, title: str, body: str) -> int:
        for item in self.pages(f"{self.prefix}/issues?state=all&sort=created&direction=desc", limit=3):
            if marker(key) in (item.get("body") or "") and "pull_request" not in item:
                return item["number"]
        created = self.api(f"{self.prefix}/issues", "POST", {"title": title, "body": body + "\n\n" + marker(key)})
        return created["number"]

    def find_pr(self, key: str, branch: str) -> dict | None:
        owner = self.slug.split("/")[0]
        for item in self.pages(f"{self.prefix}/pulls?state=all&head={quote(owner + ':' + branch, safe='')}", limit=1):
            if marker(key) in (item.get("body") or ""):
                return item
        return None

    def ensure_pr(self, key: str, work: Path, branch: str, head: str, title: str, body: str) -> dict:
        """Push the tested commit as ``branch`` and open a PR for it; idempotent by the key's marker."""
        head_sha(head)
        git(work, "check-ref-format", f"refs/heads/{branch}")
        existing = self.find_pr(key, branch)
        if existing and existing["head"]["sha"] == head and existing["base"]["ref"] == self.base:
            return self.brief(existing)
        if existing and (existing["state"] != "open" or existing["base"]["ref"] != self.base or subprocess.run(
                [GIT, *SAFE_GIT, "merge-base", "--is-ancestor", existing["head"]["sha"], head], cwd=work).returncode):
            raise PublishError(f"PR #{existing['number']} for this ticket is closed or has other history; it was not touched")
        if git(work, "rev-parse", "HEAD").strip() != head:
            raise PublishError("workspace HEAD is not the tested commit")
        rewrites = subprocess.run([GIT, *SAFE_GIT, "config", "--get-regexp", r"^url\..*\.(insteadof|pushinsteadof)$"], cwd=work,
                                  capture_output=True, text=True)
        if rewrites.stdout.strip():
            raise PublishError("Git URL rewrite configuration could redirect the push; publish manually")
        remote = self.api(f"{self.prefix}/git/matching-refs/heads/{quote(branch)}")
        ours = existing["head"]["sha"] if existing else head  # a later round fast-forwards its own PR branch
        taken = [r for r in remote if r.get("ref") == f"refs/heads/{branch}" and r["object"]["sha"] not in (head, ours)]
        if taken:
            raise PublishError(f"branch {branch} already exists on GitHub with other commits; it was not touched")
        git(work, "-c", "credential.helper=", "-c", f"credential.helper=!{GH} auth git-credential", "push", "--porcelain",
            f"https://github.com/{self.slug}.git", f"HEAD:refs/heads/{branch}")
        if existing:
            self.api(f"{self.prefix}/pulls/{existing['number']}", "PATCH", {"body": body + "\n\n" + marker(key)})
            return {**self.brief(existing), "head": head}
        try:
            item = self.api(f"{self.prefix}/pulls", "POST", {"title": title, "body": body + "\n\n" + marker(key),
                                                            "head": branch, "base": self.base})
        except PublishError:
            item = self.find_pr(key, branch)
            if not item or item["head"]["sha"] != head:
                raise
        return self.brief(item)

    @staticmethod
    def brief(item: dict) -> dict:
        return {"number": item["number"], "url": item.get("html_url"), "head": item["head"]["sha"], "branch": item["head"]["ref"]}

    def record_status(self, head: str, passed: bool, description: str) -> None:
        """The harness's measured result on the PR commit. It complements, never replaces, the repository's own CI."""
        self.api(f"{self.prefix}/statuses/{head_sha(head)}", "POST",
                 {"state": "success" if passed else "failure", "context": STATUS_CONTEXT, "description": description[:140]})

    def inspect_pr(self, number: int) -> dict:
        pr = self.api(f"{self.prefix}/pulls/{int(number)}")
        head = head_sha(pr["head"]["sha"])
        if pr["base"]["ref"] != self.base:
            raise PublishError("pull request targets an unexpected base")
        base = quote(self.base, safe="")
        branch = self.api(f"{self.prefix}/branches/{base}")
        rules = self.pages(f"{self.prefix}/rules/branches/{base}")
        try:
            protection = self.api(f"{self.prefix}/branches/{base}/protection")
        except APIError as exc:
            if exc.status != 404:
                raise
            protection = {}
        # A 404 for protection can also mean "not allowed to read it"; only an unprotected branch proves there is none.
        policy_known = not branch.get("protected") or bool(protection or rules)
        supported = {"deletion", "non_fast_forward", "creation", "update", "required_signatures", "required_linear_history",
                     "required_status_checks", "pull_request"}
        if any(rule.get("type") not in supported for rule in rules):
            policy_known = False
        legacy = protection.get("required_status_checks") or {}
        required = [c["context"] for c in legacy.get("checks", [])] or list(legacy.get("contexts", []))
        required_reviews = (protection.get("required_pull_request_reviews") or {}).get("required_approving_review_count", 0)
        for rule in rules:
            parameters = rule.get("parameters", {})
            if rule["type"] == "required_status_checks":
                required += [c["context"] for c in parameters.get("required_status_checks", [])]
            elif rule["type"] == "pull_request":
                required_reviews = max(required_reviews, parameters.get("required_approving_review_count", 0))
        runs = self.pages(f"{self.prefix}/commits/{head}/check-runs?filter=latest", "check_runs")
        statuses = self.api(f"{self.prefix}/commits/{head}/status?per_page=100").get("statuses", [])
        checks = [(r["name"], r.get("status") == "completed" and r.get("conclusion") in ("success", "neutral", "skipped")) for r in runs]
        checks += [(s["context"], s.get("state") == "success") for s in statuses]
        passed = bool(checks) and all(ok for _, ok in checks) and all(any(n == name for n, _ in checks) for name in required)
        owner, name = self.slug.split("/")
        graph = self.api("graphql", "POST", {"query": "query($o:String!,$n:String!,$p:Int!){repository(owner:$o,name:$n){pullRequest(number:$p)"
                                                      "{headRefOid reviewDecision mergeStateStatus}}}",
                                             "variables": {"o": owner, "n": name, "p": int(number)}})
        decision = ((graph.get("data") or {}).get("repository") or {}).get("pullRequest") or {}
        if decision.get("headRefOid") != head:
            policy_known = False
        reviews_satisfied = decision.get("reviewDecision") in (None, "APPROVED") if required_reviews == 0 else \
            decision.get("reviewDecision") == "APPROVED"
        mergeable = pr.get("state") == "open" and pr.get("mergeable") is True and \
            decision.get("mergeStateStatus") in ("CLEAN", "HAS_HOOKS", "UNSTABLE")
        return {"head": head, "base_head": branch["commit"]["sha"], "checks_present": bool(checks), "checks_passed": passed,
                "check_names": sorted({n for n, _ in checks}), "failed_checks": sorted({n for n, ok in checks if not ok}),
                "required_checks": sorted(set(required)), "reviews_satisfied": bool(reviews_satisfied), "mergeable": mergeable,
                "draft": pr.get("draft", True), "policy_known": bool(policy_known), "merged": pr.get("merged") is True,
                "merge_commit": pr.get("merge_commit_sha"), "state": pr.get("state")}

    def merge(self, number: int, approved_head: str) -> dict:
        snapshot = self.inspect_pr(number)
        if snapshot["merged"]:
            return {"merged": True, "merge_commit": snapshot["merge_commit"]}
        reasons = merge_gate(snapshot, approved_head, snapshot["required_checks"])
        if reasons:
            return {"merged": False, "reasons": reasons, "pending": pending_only(reasons), "snapshot": snapshot}
        try:
            self.api(f"{self.prefix}/pulls/{int(number)}/merge", "PUT", {"sha": approved_head, "merge_method": "merge"})
        except APIError as exc:
            return {"merged": False, "reasons": [f"GitHub refused the merge: {exc}"], "pending": exc.status == 405, "snapshot": snapshot}
        observed = self.api(f"{self.prefix}/pulls/{int(number)}")
        if observed.get("merged") is not True or observed["head"]["sha"] != approved_head:
            raise PublishError("GitHub has not confirmed the merge of the approved commit")
        return {"merged": True, "merge_commit": observed.get("merge_commit_sha")}


class Local:
    """For repositories without GitHub: the tested commit becomes branch ``ge/<ticket>`` in the person's checkout,
    and a merge fast-forwards the base branch only when that cannot disturb uncommitted work."""
    kind = "local"

    def __init__(self, repo: Path, base: str):
        self.repo, self.base = Path(repo), base

    def ensure_pr(self, key: str, work: Path, branch: str, head: str, title: str, body: str) -> dict:
        head_sha(head)
        local = f"ge/{key}"
        git(self.repo, "fetch", "-q", str(work), f"+{head}:refs/heads/{local}")
        return {"number": None, "url": None, "head": head, "branch": local}

    def record_status(self, head: str, passed: bool, description: str) -> None:
        return None

    def merge(self, branch: str, approved_head: str) -> dict:
        head = git(self.repo, "rev-parse", f"refs/heads/{branch}").strip()
        if head != approved_head:
            return {"merged": False, "reasons": ["branch moved since approval"], "pending": False}
        base_head = git(self.repo, "rev-parse", f"refs/heads/{self.base}").strip()
        if subprocess.run([GIT, *SAFE_GIT, "merge-base", "--is-ancestor", head, base_head], cwd=self.repo).returncode == 0:
            return {"merged": True, "merge_commit": base_head}
        if subprocess.run([GIT, *SAFE_GIT, "merge-base", "--is-ancestor", base_head, head], cwd=self.repo).returncode:
            return {"merged": False, "reasons": [f"{self.base} has moved on; the change needs another round on the new base"],
                    "pending": False}
        current = git(self.repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).strip()
        if current == self.base:
            if git(self.repo, "status", "--porcelain", "--untracked-files=no").strip():
                return {"merged": False, "reasons": [f"{self.base} is checked out with uncommitted changes"], "pending": False}
            git(self.repo, "merge", "-q", "--ff-only", head)
        else:
            git(self.repo, "update-ref", f"refs/heads/{self.base}", head, base_head)
        return {"merged": True, "merge_commit": head}
