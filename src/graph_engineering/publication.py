"""Idempotent publication and fail-closed, commit-bound merge decisions.

LocalBackend simulates GitHub objects but performs actual local Git merges.
GitHubBackend uses normal authenticated REST/GraphQL endpoints, never admin bypass.
The controller owns approval and must call merge_gate immediately before merge.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import quote
from .runtime import remaining_timeout
from .processes import background_options


class PublicationError(RuntimeError):
    """Publication is unavailable, ambiguous, or forbidden by current policy."""


class APIError(PublicationError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def merge_gate(snapshot: dict, approved_head: str | None, tested_head: str | None,
               review_ok: bool, trusted_auto: bool = False, risk: str = "low",
               required_checks: list[str] | None = None) -> list[str]:
    """Return every blocking reason; missing evidence is always a failure."""
    reasons = []
    head = snapshot.get("head")
    if not head or not re.fullmatch(r"[a-fA-F0-9]{40,64}", head):
        reasons.append("invalid PR head")
    if not head or tested_head != head:
        reasons.append("independent tests missing or stale")
    if not review_ok:
        reasons.append("independent diff review failed or missing")
    if not (head and approved_head == head) and not (trusted_auto and risk == "low"):
        reasons.append("human approval missing or stale")
    for key, message in (
        ("checks_present", "no observed checks"),
        ("checks_passed", "checks failed or pending"),
        ("reviews_satisfied", "required repository reviews unsatisfied"),
        ("mergeable", "PR conflicting, stale, or mergeability unknown"),
        ("policy_known", "repository policy unreadable or unsupported"),
    ):
        if snapshot.get(key) is not True:
            reasons.append(message)
    if snapshot.get("draft") is not False:
        reasons.append("PR is draft or draft status unknown")
    missing = set(required_checks or []) - set(snapshot.get("check_names", []))
    if missing:
        reasons.append("missing required checks: " + ", ".join(sorted(missing)))
    return reasons


def _marker(key: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", key):
        raise PublicationError("invalid idempotency key")
    return f"<!-- graph-engineering:{key} -->"


def _head(value: str) -> str:
    if not re.fullmatch(r"[a-fA-F0-9]{40,64}", value):
        raise PublicationError("invalid commit SHA")
    return value


def _possible_duplicate(existing, title, body):
    if existing.get("state") != "open":
        return False
    normalize = lambda text: " ".join(text.casefold().split())
    if normalize(existing.get("title", "")) == normalize(title):
        return True
    location = re.search(r"(?m)^Location: (.+):\d+$", body)
    tests = set(re.findall(r"\btest_[A-Za-z0-9_]+", body))
    prior = existing.get("body") or ""
    return bool(location and location[1] in prior and tests and any(test in prior for test in tests))


def _git(repo: Path, *args: str) -> str:
    # Do not execute repository-configured hooks or external diff commands.
    cmd = ["git", "-c", "core.hooksPath=NUL" if os.name == "nt" else "core.hooksPath=/dev/null",
           "-c", "core.fsmonitor=false", "-c", "protocol.ext.allow=never",
           "-C", str(repo), *args]
    from .repository import clean_environment
    env = clean_environment()
    # Read normalisation configuration but never pass controller tokens to local Git.
    env.pop("GIT_CONFIG_GLOBAL", None)
    env.pop("GIT_CONFIG_NOSYSTEM", None)
    for key in ("USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA"):
        if key in os.environ:
            env[key] = os.environ[key]
    try:
        process = subprocess.run(cmd, text=True, encoding="utf-8", errors="replace",
                                 capture_output=True, timeout=remaining_timeout(90), check=False, env=env,**background_options())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PublicationError(f"Git execution failed: {type(exc).__name__}") from exc
    if process.returncode:
        raise PublicationError(f"Git {args[0]} failed (exit {process.returncode})")
    return process.stdout.strip()


class LocalBackend:
    """Explicit local simulation with persistent objects and real Git commits.

    Requires the controller's repository lock. This backend never pushes a remote.
    """
    kind = "local"

    def __init__(self, root: Path, repo: Path, base: str):
        self.root, self.repo, self.base = Path(root), Path(repo).resolve(), base
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "local-github.json"
        _git(self.repo, "check-ref-format", "--branch", base)

    def _load(self):
        if not self.path.exists():
            return dict(backend="local-simulation", repo=str(self.repo), base=self.base,
                        issues=[], prs=[], reviews=[])
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise PublicationError("local publication state unreadable") from exc
        if state.get("repo") != str(self.repo) or state.get("base") != self.base:
            raise PublicationError("local backend state belongs to another repository/base")
        return state

    def _save(self, state):
        temporary = self.path.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)

    @staticmethod
    def _find(state, collection, number):
        matches = [item for item in state[collection] if item["number"] == number]
        if len(matches) != 1:
            raise PublicationError(f"unknown or duplicate local {collection} object")
        return matches[0]

    @staticmethod
    def _brief(item):
        return {key: item[key] for key in ("number", "url", "key", "head") if key in item}

    def ensure_issue(self, key, title, body, labels=None):
        marker = _marker(key)
        state = self._load()
        matches = [item for item in state["issues"] if item["key"] == key]
        if len(matches) > 1:
            raise PublicationError("duplicate local issues")
        if matches:
            return self._brief(matches[0])
        if any(_possible_duplicate(item, title, body) for item in state["issues"]):
            raise PublicationError("Possible existing issue requires supervisor linkage; refusing duplicate creation")
        number = max([item["number"] for item in state["issues"]] + [0]) + 1
        item = dict(number=number, url=f"local://issues/{number}", key=key,
                    title=title, body=body + "\n\n" + marker,
                    labels=labels or [], state="open")
        state["issues"].append(item)
        self._save(state)
        return self._brief(item)

    def reconcile_issue(self, key):
        matches = [item for item in self._load()["issues"] if item["key"] == key]
        if len(matches) != 1:
            raise PublicationError("Uncertain issue outcome; no unique observed issue, refusing creation")
        return self._brief(matches[0])

    def reconcile_pr(self, key, branch, head):
        matches = [item for item in self._load()["prs"] if item["key"] == key and item["branch"] == branch and item["head"] == head]
        if len(matches) != 1:
            raise PublicationError("Uncertain PR outcome; refusing creation")
        return self._brief(matches[0])

    def update_issue(self, number, body=None, labels=None, state=None, title=None):
        data = self._load()
        item = self._find(data, "issues", number)
        if title is not None:item['title']=title
        if body is not None:
            item["body"] = body + "\n\n" + _marker(item["key"])
        if labels is not None:
            item["labels"] = labels
        if state is not None:
            if state not in ("open", "closed"):
                raise PublicationError("invalid issue state")
            item["state"] = state
        self._save(data)
        return self._brief(item)

    def ensure_pr(self, key, title, body, branch, head):
        _marker(key)
        _head(head)
        _git(self.repo, "check-ref-format", "--branch", branch)
        if _git(self.repo, "rev-parse", "--verify", f"refs/heads/{branch}") != head:
            raise PublicationError("branch head differs from tested commit")
        data = self._load()
        matches = [item for item in data["prs"] if item["key"] == key or item["branch"] == branch]
        if len(matches) > 1:
            raise PublicationError("duplicate local pull requests")
        if matches:
            item = matches[0]
            if item["head"] != head or item["key"] != key:
                raise PublicationError("existing PR belongs to a different task/head")
            return self._brief(item)
        number = max([item["number"] for item in data["prs"]] + [0]) + 1
        item = dict(number=number, url=f"local://pulls/{number}", key=key,
                    title=title, body=body + "\n\n" + _marker(key), branch=branch,
                    head=head, merged=False, merge_commit=None, checks=None)
        data["prs"].append(item)
        self._save(data)
        return self._brief(item)

    def record_check(self, number, head, passed, name="local-tests"):
        data = self._load()
        item = self._find(data, "prs", number)
        if item["head"] != head or type(passed) is not bool:
            raise PublicationError("check evidence does not match PR head")
        item["checks"] = dict(head=head, passed=passed, name=name)
        self._save(data)

    def inspect_pr(self, number):
        data = self._load()
        item = self._find(data, "prs", number)
        base_head = _git(self.repo, "rev-parse", "--verify", f"refs/heads/{self.base}")
        actual_head = _git(self.repo, "rev-parse", "--verify", f"refs/heads/{item['branch']}")
        check = item.get("checks") or {}
        valid = check.get("head") == actual_head == item["head"]
        # A merge intent survives a controller crash between Git and JSON writes.
        intent = item.get("merge_intent")
        if intent and not item["merged"] and base_head != intent["base_before"]:
            try:
                _git(self.repo, "merge-base", "--is-ancestor", item["head"], base_head)
            except PublicationError:
                pass
            else:
                item.update(merged=True, merge_commit=base_head)
                self._save(data)
        try:
            _git(self.repo, "merge-base", "--is-ancestor", base_head, actual_head)
            mergeable = True
        except PublicationError:
            mergeable = bool(item["merged"])
        return dict(backend="local-simulation", head=actual_head, base_head=base_head,
                    checks_present=bool(valid), checks_passed=bool(valid and check.get("passed") is True),
                    check_names=[check["name"]] if valid else [], reviews_satisfied=True,
                    mergeable=mergeable, draft=False, policy_known=True,
                    merged=item["merged"], merge_commit=item["merge_commit"])

    def publish_review(self, number, body):
        data = self._load()
        self._find(data, "prs", number)
        item = dict(number=number, body=body, event="COMMENT", backend="local-simulation")
        if item not in data["reviews"]:
            data["reviews"].append(item)
            self._save(data)
        return item

    def reconcile_review(self, number, body):
        matches = [item for item in self._load()["reviews"] if item["number"] == number and item["body"] == body]
        if len(matches) != 1:
            raise PublicationError("Uncertain review-comment outcome; refusing another write")
        return matches[0]

    def merge(self, number, head):
        _head(head)
        snapshot = self.inspect_pr(number)
        if snapshot["head"] != head:
            raise PublicationError("PR head changed before merge")
        if snapshot["merged"]:
            return dict(merged=True, merge_commit=snapshot["merge_commit"], backend="local-simulation")
        if not all(snapshot[name] for name in ("checks_present", "checks_passed", "mergeable")):
            raise PublicationError("local checks failed/missing or base changed")
        if _git(self.repo, "status", "--porcelain"):
            raise PublicationError("local merge requires a clean repository")
        if _git(self.repo, "branch", "--show-current") != self.base:
            raise PublicationError("local merge requires the checked out base branch")
        data = self._load()
        item = self._find(data, "prs", number)
        item["merge_intent"] = dict(head=head, base_before=snapshot["base_head"])
        self._save(data)
        _git(self.repo, "-c", "user.name=Graph Engineering", "-c",
             "user.email=graph-engineering@localhost", "merge", "--no-ff", "--no-edit", head)
        merge_commit = _git(self.repo, "rev-parse", "HEAD")
        item.update(merged=True, merge_commit=merge_commit)
        self._save(data)
        return dict(merged=True, merge_commit=merge_commit, backend="local-simulation")


class GitHubBackend:
    """GitHub publication through gh, using keyring credentials only in controller."""
    kind = "github"

    def __init__(self, root: Path, repo: Path, slug: str, base: str):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", slug):
            raise PublicationError("invalid GitHub repository slug")
        self.root, self.repo, self.slug, self.base = Path(root), Path(repo), slug, base
        self.prefix = f"repos/{slug}"

    def _api(self, endpoint, method="GET", payload=None):
        cmd = ["gh", "api", "--hostname", "github.com", "--method", method,
               "-H", "Accept: application/vnd.github+json", endpoint]
        if payload is not None:
            cmd += ["--input", "-"]
        if method == "GET":
            cmd += ["--header", "Cache-Control: no-cache", "--header", "Pragma: no-cache"]
        try:
            result = subprocess.run(cmd, input=json.dumps(payload) if payload is not None else None,
                                    text=True, encoding="utf-8", errors="replace", capture_output=True,
                                    timeout=remaining_timeout(60), check=False,**background_options())
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PublicationError(f"GitHub API unavailable: {type(exc).__name__}") from exc
        if result.returncode:
            match = re.search(r"HTTP (\d{3})", result.stderr)
            status = int(match.group(1)) if match else None
            raise APIError(f"GitHub API {method} failed ({status or 'transport error'})", status)
        try:
            return json.loads(result.stdout) if result.stdout.strip() else {}
        except ValueError as exc:
            raise PublicationError("GitHub returned invalid JSON") from exc

    def _list(self, endpoint, field=None):
        values = []
        separator = "&" if "?" in endpoint else "?"
        for page in range(1, 101):
            data = self._api(f"{endpoint}{separator}per_page=100&page={page}")
            items = data.get(field) if field else data
            if not isinstance(items, list):
                raise PublicationError("unexpected GitHub pagination response")
            values.extend(items)
            if len(items) < 100:
                return values
        raise PublicationError("GitHub pagination bound exceeded; manual reconciliation required")

    @staticmethod
    def _brief(item, key=None):
        result = dict(number=item["number"], url=item.get("html_url", ""))
        if key:
            result["key"] = key
        if "head" in item:
            result["head"] = item["head"]["sha"]
            if item["head"].get("ref"):
                result["branch"] = item["head"]["ref"]  # The name the change is published under.
        return result

    def remote_branch_head(self, name):
        """The commit a branch of that name points to on GitHub, or None when no such branch exists."""
        _git(self.repo, "check-ref-format", f"refs/heads/{name}")
        listed = _git(self.repo, "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential",
                      "ls-remote", "--heads", f"https://github.com/{self.slug}.git", f"refs/heads/{name}")
        for line in listed.splitlines():
            sha, _, ref = line.partition("\t")
            if ref.strip() == f"refs/heads/{name}":
                return _head(sha.strip())
        return None

    def _matches(self, collection, key, branch=None):
        marker = _marker(key)
        items = self._list(f"{self.prefix}/{collection}?state=all")
        matches = [item for item in items if marker in (item.get("body") or "") or
                   (branch and item.get("head", {}).get("ref") == branch)]
        if collection == "issues":
            matches = [item for item in matches if "pull_request" not in item]
        if len(matches) > 1:
            raise PublicationError("duplicate GitHub markers/branches; manual reconciliation required")
        return matches

    def ensure_issue(self, key, title, body, labels=None):
        matches = self._matches("issues", key)
        if matches:
            return self._brief(matches[0], key)
        for item in self._list(f"{self.prefix}/issues?state=open"):
            if "pull_request" not in item and _possible_duplicate(item, title, body):
                raise PublicationError(f"Possible existing issue #{item['number']} requires supervisor linkage; refusing duplicate creation")
        try:
            item = self._api(f"{self.prefix}/issues", "POST",
                             dict(title=title, body=body + "\n\n" + _marker(key), labels=labels or []))
        except PublicationError:
            # POST may have succeeded before a connection loss. Observe before retrying.
            matches = self._matches("issues", key)
            if not matches:
                raise
            item = matches[0]
        return self._brief(item, key)

    def inspect_repository(self):
        metadata = self._api(self.prefix)
        result = {"default_branch": metadata.get("default_branch"), "permissions": metadata.get("permissions", {}),
                  "archived": metadata.get("archived"), "visibility": metadata.get("visibility"), "policy_reads": {}}
        for name, suffix in (("branch", ""), ("protection", "/protection"), ("rules", None)):
            endpoint = (f"{self.prefix}/rules/branches/{quote(self.base, safe='')}" if suffix is None
                        else f"{self.prefix}/branches/{quote(self.base, safe='')}{suffix}")
            try:
                result["policy_reads"][name] = {"observed": True, "data": self._api(endpoint)}
            except PublicationError as exc:
                result["policy_reads"][name] = {"observed": False, "error": str(exc)}
        return result

    def reconcile_issue(self, key):
        matches = self._matches("issues", key)
        if len(matches) != 1:
            raise PublicationError("Uncertain issue outcome; no unique observed issue, refusing creation. Resume after remote visibility is confirmed")
        return self._brief(matches[0], key)

    def reconcile_pr(self, key, branch, head, remote_branch=None):
        published = remote_branch or branch
        matches = self._matches("pulls", key, published)
        if len(matches) != 1:
            raise PublicationError("Uncertain PR outcome; no unique observed PR, refusing creation")
        item = matches[0]
        if (_marker(key) not in (item.get("body") or "") or item["head"]["sha"] != head
                or item["head"]["ref"] != published or item["base"]["ref"] != self.base):
            raise PublicationError("Reconciled PR has conflicting ownership/base/head")
        return self._brief(item, key)

    def update_issue(self, number, body=None, labels=None, state=None, title=None):
        payload = {}
        if title is not None:payload['title']=title
        if body is not None:
            current = self._api(f"{self.prefix}/issues/{int(number)}")
            markers = re.findall(r"<!-- graph-engineering:[A-Za-z0-9_.:-]+ -->", current.get("body") or "")
            payload["body"] = body + "\n\n" + "\n".join(markers)
        if labels is not None:
            payload["labels"] = labels
        if state is not None:
            if state not in ("open", "closed"):
                raise PublicationError("invalid issue state")
            payload["state"] = state
        return self._api(f"{self.prefix}/issues/{int(number)}", "PATCH", payload)

    def ensure_pr(self, key, title, body, branch, head, remote_branch=None):
        """``remote_branch``: the name the tested local ``branch`` is published under (a repository's branch
        convention such as ``feat/17-model-name``); the PR head is that name."""
        _head(head)
        _git(self.repo, "check-ref-format", "--branch", branch)
        published = remote_branch or branch
        if published != branch:
            _git(self.repo, "check-ref-format", f"refs/heads/{published}")  # A full ref name: no shorthand expansion.
        matches = self._matches("pulls", key, published)
        if matches:
            item = matches[0]
            if (_marker(key) not in (item.get("body") or "") or item["head"]["sha"] != head
                    or item["head"]["ref"] != published or item["base"]["ref"] != self.base):
                raise PublicationError("existing PR belongs to another task/base/head")
            return self._brief(item, key)
        if _git(self.repo, "rev-parse", "--verify", f"refs/heads/{branch}") != head:
            raise PublicationError("local branch no longer matches tested commit")
        remote = _git(self.repo, "remote", "get-url", "origin")
        if not re.fullmatch(r"(?:https://github\.com/|git@github\.com:)" + re.escape(self.slug) + r"(?:\.git)?", remote):
            raise PublicationError("origin does not match the configured GitHub repository")
        # Refuse URL rewriting; never let origin.pushurl redirect a privileged push.
        rewrites = subprocess.run(["git", "-C", str(self.repo), "config", "--get-regexp",
                                  r"^url\..*\.(insteadof|pushinsteadof)$"], capture_output=True, text=True, timeout=10,**background_options())
        if rewrites.returncode not in (0, 1) or rewrites.stdout.strip():
            raise PublicationError("Git URL rewrite configuration requires manual publication")
        if published != branch:
            existing = self.remote_branch_head(published)
            if existing not in (None, head):  # A person's branch of the same name: never pushed over, named clearly.
                raise PublicationError(f"remote branch {published} already exists with other history ({existing[:12]}); "
                                       "it is not touched, choose another published name")
        _git(self.repo, "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential",
             "push", "--porcelain", f"https://github.com/{self.slug}.git", f"refs/heads/{branch}:refs/heads/{published}")
        try:
            item = self._api(f"{self.prefix}/pulls", "POST", dict(
                title=title, body=body + "\n\n" + _marker(key), head=published, base=self.base))
        except PublicationError:
            matches = self._matches("pulls", key, published)
            if not matches:
                raise
            item = matches[0]
            if item["head"]["sha"] != head or _marker(key) not in (item.get("body") or ""):
                raise PublicationError("uncertain PR creation found conflicting ownership/head")
        return self._brief(item, key)

    def inspect_pr(self, number):
        pr = self._api(f"{self.prefix}/pulls/{int(number)}")
        head = _head(pr["head"]["sha"])
        if pr["base"]["ref"] != self.base:
            raise PublicationError("pull request targets an unexpected base")
        branch = self._api(f"{self.prefix}/branches/{quote(self.base, safe='')}")
        rules = self._list(f"{self.prefix}/rules/branches/{quote(self.base, safe='')}")
        try:
            protection = self._api(f"{self.prefix}/branches/{quote(self.base, safe='')}/protection")
        except APIError as exc:
            if exc.status != 404:
                raise
            protection = {}
        # Branch-rules API includes inherited active rulesets. Do not infer no policy
        # from a 404 alone; private/inaccessible protection endpoints also return 404.
        policy_known = not branch.get("protected") or bool(protection or rules)
        supported = {"deletion", "non_fast_forward", "creation", "update", "required_signatures",
                     "required_linear_history", "required_status_checks", "pull_request"}
        if any(rule.get("type") not in supported for rule in rules):
            policy_known = False
        required = []
        legacy = protection.get("required_status_checks") or {}
        if legacy.get("checks"):
            required.extend((value["context"], value.get("app_id")) for value in legacy["checks"])
        else:
            required.extend((context, None) for context in legacy.get("contexts", []))
        strict = legacy.get("strict", False)
        required_reviews = (protection.get("required_pull_request_reviews") or {}).get("required_approving_review_count", 0)
        for rule in rules:
            parameters = rule.get("parameters", {})
            if rule["type"] == "required_status_checks":
                required.extend((item["context"], item.get("integration_id"))
                                for item in parameters.get("required_status_checks", []))
                strict = strict or parameters.get("strict_required_status_checks_policy", False)
            elif rule["type"] == "pull_request":
                required_reviews = max(required_reviews, parameters.get("required_approving_review_count", 0))
        runs = self._list(f"{self.prefix}/commits/{head}/check-runs?filter=latest", "check_runs")
        status = self._api(f"{self.prefix}/commits/{head}/status?per_page=100")
        if status.get("total_count", 0) > 100:
            raise PublicationError("too many commit statuses for a reliable policy snapshot")
        checks = [(item["name"], (item.get("app") or {}).get("id"),
                   item.get("status") == "completed" and item.get("conclusion") in ("success", "neutral", "skipped"))
                  for item in runs]
        checks.extend((item["context"], None, item.get("state") == "success") for item in status.get("statuses", []))
        all_passed = bool(checks) and all(item[2] for item in checks)
        for name, app in required:
            matching = [item for item in checks if item[0] == name and (app in (None, -1) or item[1] == app)]
            if not matching or not all(item[2] for item in matching):
                all_passed = False
        owner, name = self.slug.split("/")
        query = """query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){headRefOid baseRefOid reviewDecision mergeStateStatus}}}"""
        graph = self._api("graphql", "POST", dict(query=query, variables=dict(owner=owner, name=name, number=int(number))))
        if graph.get("errors"):
            raise PublicationError("GraphQL policy evidence unavailable")
        try:
            decision = graph["data"]["repository"]["pullRequest"]
        except (KeyError, TypeError) as exc:
            raise PublicationError("GraphQL policy evidence missing") from exc
        if decision["headRefOid"] != head or decision["baseRefOid"] != branch["commit"]["sha"]:
            policy_known = False
        reviews = self._list(f"{self.prefix}/pulls/{int(number)}/reviews")
        latest = {}
        for review in reviews:
            if review.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
                latest[review["user"]["login"]] = review
        valid_approvals = [item for login, item in latest.items() if login != pr["user"]["login"]
                           and item.get("commit_id") == head and item["state"] == "APPROVED"]
        requested_changes = any(item["state"] == "CHANGES_REQUESTED" for item in latest.values())
        policy_reviews = required_reviews > 0 or bool(protection.get("required_pull_request_reviews")) or any(
            rule.get("type") == "pull_request" for rule in rules)
        reviews_satisfied = not requested_changes and len(valid_approvals) >= required_reviews
        if policy_reviews:
            reviews_satisfied = reviews_satisfied and decision.get("reviewDecision") == "APPROVED"
        mergeable = pr.get("mergeable") is True and pr.get("state") == "open"
        if strict and pr.get("mergeable_state") == "behind":
            mergeable = False
        if decision.get("mergeStateStatus") not in ("CLEAN", "HAS_HOOKS", "UNSTABLE"):
            mergeable = False
        return dict(backend="github", head=head, base_head=branch["commit"]["sha"],
                    checks_present=bool(checks), checks_passed=all_passed,
                    check_names=sorted({item[0] for item in checks}),
                    reviews_satisfied=bool(reviews_satisfied), mergeable=mergeable,
                    draft=pr.get("draft", True), policy_known=bool(policy_known),
                    merged=pr.get("merged") is True, merge_commit=pr.get("merge_commit_sha"))

    def publish_review(self, number, body):
        """Publish evidence as COMMENT, never impersonate an independent approval."""
        endpoint = f"{self.prefix}/pulls/{int(number)}/reviews"
        matches = [item for item in self._list(endpoint) if item.get("body") == body]
        if matches:
            return matches[-1]

        pr = self._api(f"{self.prefix}/pulls/{int(number)}")
        try:
            return self._api(endpoint, "POST", dict(body=body, event="COMMENT", commit_id=pr["head"]["sha"]))
        except PublicationError:
            matches = [item for item in self._list(endpoint) if item.get("body") == body]
            if not matches:
                raise
            return matches[-1]

    def reconcile_review(self, number, body):
        matches = [item for item in self._list(f"{self.prefix}/pulls/{int(number)}/reviews") if item.get("body") == body]
        if len(matches) != 1:
            raise PublicationError("Uncertain review-comment outcome; refusing another write")
        return matches[0]

    def record_check(self, number, head, passed, name="graph-engineering/independent-tests"):
        """Publish controller-observed tests, bound to an unchanged PR commit.

        This status is not a substitute for configured external CI or required
        GitHub App checks. The caller must possess actual independent test evidence.
        """
        _head(head)
        if type(passed) is not bool or not isinstance(name, str) or not 1 <= len(name) <= 100:
            raise PublicationError("invalid test status evidence")
        pr = self._api(f"{self.prefix}/pulls/{int(number)}")
        if pr.get("head", {}).get("sha") != head:
            raise PublicationError("test status head no longer matches PR")
        state = "success" if passed else "failure"
        statuses = self._list(f"{self.prefix}/commits/{head}/statuses")
        current = next((item for item in statuses if item.get("context") == name), None)
        if current and current.get("state") == state:
            return current
        payload = dict(state=state, context=name,
                       description="Controller observed independent tests " + ("passing" if passed else "failing"))
        try:
            return self._api(f"{self.prefix}/statuses/{head}", "POST", payload)
        except PublicationError:
            statuses = self._list(f"{self.prefix}/commits/{head}/statuses")
            current = next((item for item in statuses if item.get("context") == name), None)
            if current and current.get("state") == state:
                return current
            raise

    def merge(self, number, head):
        _head(head)
        snapshot = self.inspect_pr(number)
        if snapshot["head"] != head:
            raise PublicationError("PR head changed before merge")
        if snapshot["merged"]:
            return dict(merged=True, merge_commit=snapshot["merge_commit"], backend="github")
        reasons = merge_gate(snapshot, head, head, True)
        if reasons:
            raise PublicationError("merge refused: " + "; ".join(reasons))
        try:
            result = self._api(f"{self.prefix}/pulls/{int(number)}/merge", "PUT",
                               dict(sha=head, merge_method="merge"))
        except PublicationError:
            result = {}
        observed = self._api(f"{self.prefix}/pulls/{int(number)}")
        if observed.get("merged") is not True or observed.get("head", {}).get("sha") != head:
            raise PublicationError("server has not confirmed the requested merge")
        commit = observed.get("merge_commit_sha")
        if not commit or (result.get("sha") and result["sha"] != commit):
            raise PublicationError("merged commit evidence is missing or inconsistent")
        return dict(merged=True, merge_commit=commit, backend="github")
