# pr

(Vibe-coded) Stacked-PR manager for GitHub. State at `~/.pr.json`, keyed by
git toplevel.  Requires `gh` on `$PATH`.

Install (Python 3.10+):

```
pipx install git+https://github.com/dtebbs/pr.git   # or: uv tool install git+...
pipx upgrade pr                                     # later, to update
```

Type:

```
pr --help
```

for options.  `pr show` is a pure render of cached state — offline. Run `pr
fetch` to refresh.  Teammates' PRs are flagged `(external)`.

Tests: `make test`.
