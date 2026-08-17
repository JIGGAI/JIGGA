# Dev auto-deploy

On the dev box, whatever ref a target is pinned to is what runs. Unpinned means
`main`, so merging is the deploy.

```bash
./scripts/deploy-dev.sh --install-timer   # every 2 minutes from now on
./scripts/deploy-dev.sh status            # what is running, from which ref
./scripts/deploy-dev.sh --dry-run         # what would happen, changing nothing
./scripts/deploy-dev.sh                   # deploy now
tail -f ~/.jigga/logs/deploy.log
```

## Running a feature branch here

```bash
./scripts/deploy-dev.sh pin view feat/workflows-tab   # deploys it immediately
./scripts/deploy-dev.sh status
./scripts/deploy-dev.sh unpin view                    # back to main
```

A pin is per target, so the dashboard can run a branch while the runtime stays
on `main`, or the reverse. Once pinned, the timer keeps that branch fresh: push
to it and the box has it within a couple of minutes, same as `main`.

Pins live in `~/.jigga/state/deploy-pins` — a file rather than a flag, because
the timer runs unattended and "which branch is this box on" has to survive
between invocations and be answerable by reading one thing.

**When a pinned branch is merged and deleted, the pin releases itself.** The
deploy notices the ref is gone from origin, logs it, unpins, and returns to
`main` on the next tick. That is the normal end of a pin's life, so it should
not require remembering to clean up.

A branch with no open PR has no CI, which reads as `unknown` and deploys
anyway — you usually want to *see* a branch before it is vetted. A branch whose
CI is red is still refused; use `--skip-ci-check` when you mean it.

Deploy targets are checked out **detached** at `origin/<ref>`. They track a ref;
they are not workspaces. That makes switching branches and following a
force-push the same operation as a fast-forward, with no local branch to
diverge.

## Why it pulls instead of being pushed to

GitHub Actions cannot reach a tailnet machine, so a push-based deploy would
mean exposing a listener or running a self-hosted runner. A timer that asks
"anything new?" needs no inbound network, no credentials on GitHub, and no
port. It exits in well under a second when the answer is no.

## What it deploys

| Target | Source | Deploy step |
|---|---|---|
| `core` — the `jigga` on PATH | `~/jigga-stable` | checkout the ref, reinstall **only if `pyproject.toml` changed**, restart the supervisor |
| `view` — the dashboard | `~/jiggaview-stable` (cloned on first run) | rsync into `~/.jigga/plugins/jiggaview`, `npm ci` **only if the lockfile changed**, `npm run build`, restart the plugin service |

The dashboard is mirrored into the plugin directory rather than reinstalled:
`jigga plugins` has `install` but no `update`, and a reinstall would discard
the recorded approval and re-fetch `node_modules` for no reason.

## It will not deploy a broken commit

Each target's HEAD commit is checked against its GitHub check-runs first:

- **green** → deploy
- **red** → skip, and say so in the log. The last good deploy keeps serving
- **pending** → skip; the next tick asks again. CI that is still running is not
  a failure, it is an answer that has not arrived
- **unknown** (no CI configured, no `gh` auth, offline) → deploy anyway

Override with `--skip-ci-check` when you need a commit out regardless.

## What it refuses to do

- **Touch a dirty checkout.** If `~/jigga-stable` or `~/jiggaview-stable` has
  uncommitted changes, it logs and leaves them alone. A background timer must
  never eat someone's work in progress.
- **Run twice at once.** A `flock` means a build outlasting the timer interval
  cannot have a second run rsyncing underneath it.
- **Deploy a ref that no longer exists.** A pinned branch deleted from origin
  releases its pin and falls back to `main` rather than serving a dead ref.

## Restarts

A pull replaces code under a running daemon that already holds the old version
in memory, so the supervisor is restarted whenever core moves. `jigga service`
control from a script is safe as of JIGGA #205 — before it, injected runners
were ignored and the CLI drove the real service manager unconditionally.

Note that restarting the supervisor interrupts nothing durable: in-flight task
claims are released on drain, and anything mid-run is picked up on the next
tick.

## Turning it off

```bash
./scripts/deploy-dev.sh --uninstall-timer
```

This is a **dev-box** tool. It assumes you want `main` running here without
asking. Do not install it anywhere you would mind a merge going live two
minutes later.
