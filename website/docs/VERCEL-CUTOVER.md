# Vercel cutover: DONE 2026-08-04. This is the record.

Vercel now builds `website/` from this repo directly. The domain stayed put,
and the old personal mirror (`Jungleislander/venue-economics`) is retired.

## How it ended up working

The first attempt hit a Vercel platform rule
(<https://vercel.com/docs/git/vercel-for-github>, "Personal account
repositories"): only the OWNER of a personal-account repo can connect it to a
Vercel project; a collaborator cannot, and the repo then lived under
`mr-kelsey`. The fix: mr-kelsey created the **`venue-economics` GitHub org**
(2026-08-04), transferred this repo into it, installed the Vercel GitHub App
on the org, and invited the team. Org repos are connectable by any org member
with access, so the original dashboard cutover applied unchanged. Old
`mr-kelsey/su26-aai590-Group2` URLs and git remotes redirect; re-point clones
with:
`git remote set-url origin https://github.com/venue-economics/su26-aai590-group2.git`

## The Vercel project state (project `venue-economics`, team `steves-projects-fdb198c2`)

1. Git: connected to `venue-economics/su26-aai590-group2`.
2. Build and Deployment -> Root Directory = `website`.
3. Root Directory -> "Skip deployments when there are no changes to the root
   directory or its dependencies" = Enabled. (Native replacement for the
   custom `git diff --quiet HEAD^ HEAD -- .` ignored-build-step command an
   earlier draft of this doc prescribed.)
4. Production Branch: `main`. Merges to main deploy production; branch pushes
   build previews.
5. Build command: `npm run vercel-build` (Vercel auto-prefers it over
   `build`): fetches `src/data/pois.json` from S3, then `astro build`.

## pois.json: fetched at build, never committed

The first deploy from this repo failed: the repo-root `.gitignore`'s blanket
`data/` rule had silently kept `website/src/data/` out of PR #9. The fix
split by license posture (this repo is PUBLIC):

- `cells.json` + `giants-schedule.json`: committed (aggregated grid geometry
  and public MLB facts).
- `pois.json` (13,966 Advan-derived POIs, Dewey student license): NOT
  committed. It lives at `s3://aai-590-group2-capstone/website-data/pois.json`
  and `scripts/fetch-pois.mjs` downloads it during the build, authenticated by
  the POIS_* env vars in Vercel (IAM user `vercel-website-build`,
  `s3:GetObject` on `website-data/*` only). After regenerating the index,
  re-upload it to that key.

## Mirror retirement

1. The GitHub repo `Jungleislander/venue-economics` is archived (read-only,
   history browsable), with a final commit pointing here.
2. `scripts/sync-from-team-repo.sh` is deleted from this repo; the interim
   mirror flow note in `website/CLAUDE.md` is replaced by the section
   "Source of truth and deploy flow".
3. The local checkout `~/Projects/hyperfocus/venue-economics` on Steve's
   machine is a frozen archive; the working copy is this repo.

## Env vars

- Build: POIS_* (see `.env.example`).
- Runtime: none while the model runs in simulated preview. When the live
  endpoint ships, set `AWS_REGION` / `AWS_ACCESS_KEY_ID` /
  `AWS_SECRET_ACCESS_KEY` / `SAGEMAKER_ENDPOINT_ORACLE`
  (see docs/PLUG-IN-ENDPOINT.md). The retired `SAGEMAKER_ENDPOINT` var can be
  deleted.
