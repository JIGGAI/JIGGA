# Dev auto-deploy

On the dev box, whatever is on `main` is what runs. Merging is the deploy.

```bash
./scripts/deploy-dev.sh --install-timer   # every 2 minutes from now on
./scripts/deploy-dev.sh --dry-run         # what would happen, changing nothing
./scripts/deploy-dev.sh                   # deploy now
tail -f ~/.jigga/logs/deploy.log
```

## Why it pulls instead of being pushed to

GitHub Actions cannot reach a tailnet machine, so a push-based deploy would
mean exposing a listener or running a self-hosted runner. A timer that asks
"anything new?" needs no inbound network, no credentials on GitHub, and no
port. It exits in well under a second when the answer is no.

## What it deploys

| Target | Source | Deploy step |
|---|---|---|
| the `jigga` on PATH | `~/jigga-stable` | `git merge --ff-only`, reinstall **only if `pyproject.toml` changed**, restart the supervisor |
| the dashboard | `~/jiggaview-stable` (cloned on first run) | rsync into `~/.jigga/plugins/jiggaview`, `npm ci` **only if the lockfile changed**, `npm run build`, restart the plugin service |

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
- **Fast-forward past a divergence.** The merge is `--ff-only`, so a rewritten
  history stops the deploy instead of silently resolving it.

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
