# pr

Stacked-PR manager for GitHub. State at `~/.pr.json`, keyed by git toplevel.
Requires `gh` on `$PATH`.

```
pr branch feat/foo       # branch + record dep on current branch
pr create -m "..."       # push + open draft PR (--ready for non-draft)
pr fetch                 # refresh state with every open PR in the repo
pr                       # render the forest (mine + teammates')
pr target other          # retarget current PR's base
pr rebase                # rebase current branch onto its dep
```

`pr show` is a pure render of cached state — offline. Run `pr fetch` to refresh.
Teammates' PRs are flagged `(external)`.

Tests: `make test`.
