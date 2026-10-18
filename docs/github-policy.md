# Publication and merge policy

`publication.py` offers two deliberately distinct backends. `LocalBackend`
persists **simulated GitHub objects** in `local-github.json`; its merges are actual
Git merges in the supplied repository. `GitHubBackend` calls GitHub REST and
GraphQL using the installed `gh` executable and its authenticated keyring. It
pushes only the scoped branch to a matching `origin` and never force-pushes.
Neither backend rewrites CI, branch protection, rulesets, or security settings.

Every issue and PR contains a stable `<!-- graph-engineering:KEY -->` marker.
Creation searches all states with bounded pagination, refuses duplicate matches,
and reconciles a possibly successful POST after connection loss before retrying.
Existing PR ownership, branch, base and commit must match. Issue body updates
preserve markers. The single-host controller must hold its repository lock;
these markers are reconciliation identifiers, not a distributed uniqueness
constraint or protection against a malicious GitHub administrator.

The controller must call `merge_gate(snapshot, approved_head, tested_head,
review_ok, ...)` immediately before invoking `merge`. Human approval binds to the
exact tested PR SHA. Automatic mode must be explicitly enabled and is restricted
to changes classified low risk. Both modes require independent tests, independent
diff review, observed passing checks, current required reviews, known repository
policy, a non-draft PR and known mergeability. A missing field blocks the merge.
The controller additionally binds the current base SHA to the verified baseline;
publication alone does not certify regression tests or approval provenance.

Live inspection reads legacy branch protection and the active branch rules API
(including inherited rules), actual check runs, combined commit statuses,
independent review events and GraphQL review/merge decisions. Checks match both
name and required GitHub App ID where specified. Empty checks, pending/failed
checks, stale/self approvals, changes requested, unreadable policy, a racing head
or base, and unsupported rules block merging. A protection 404 alone is never
proof that a protected branch has no requirements. Merge queues, deployment
rules, required workflows and code-scanning rules currently require manual
handling and fail closed. Review findings are published as `COMMENT`, never as
an approval vote by the implementation identity.

The merge endpoint is the normal GitHub REST endpoint with `sha` equal to the
checked PR head. There is no admin/bypass flag. The server continues to enforce
its policy; API failure cannot become success. Completion requires an observed
`merged` PR and merge commit SHA; an uncertain response is reconciled by reading
the PR. The operator must also run post-merge validation in the graph.

Local simulation requires a clean repository checked out on its configured base,
observed commit-bound test evidence through `record_check`, and a branch descended
from that base. It persists a merge intent before running Git; a resumed controller
recognises a completed merge by ancestry and retains the observed commit. A dirty
tree, changed base, stale head or failed tests blocks local merging. Mock issue,
review and PR URLs begin with `local://`; these are never represented as live
GitHub results.

Git and `gh` use argument arrays, bounded timeouts and no shell. Git hooks,
external transport helpers and filesystem monitor commands are disabled for
backend operations. API errors expose status codes rather than response bodies
or credentials. This is process hygiene, not an OS sandbox: repository execution
must run through the controller's separate trust and credential policy.

The implementation follows GitHub's primary documentation for
[branch protection](https://docs.github.com/en/rest/branches/branch-protection),
[active branch rules](https://docs.github.com/en/rest/repos/rules#get-rules-for-a-branch),
[checks](https://docs.github.com/en/rest/checks/runs),
[PR reviews](https://docs.github.com/en/rest/pulls/reviews), and
[SHA-bound merges](https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request).
