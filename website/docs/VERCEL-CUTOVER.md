# Vercel cutover: deploy the website from THIS repo

Goal: Vercel builds `website/` from `mr-kelsey/su26-aai590-Group2` directly, the
domain stays put, and the old personal mirror repo retires. Until every step
below is done, production keeps deploying from the mirror
(`Jungleislander/venue-economics`) via `scripts/sync-from-team-repo.sh`.

## Step 1: Jonno (repo owner), one-time GitHub App install

1. Open https://github.com/apps/vercel and click Configure.
2. Pick the `mr-kelsey` account.
3. Choose "Only select repositories" and select `su26-aai590-Group2`.
4. Save. Nothing else; no Vercel account needed.

## Step 2: Steve, in the Vercel dashboard (project `venue-economics`)

1. Settings -> Git -> Disconnect the current repo
   (`Jungleislander/venue-economics`), then Connect Git Repository ->
   `mr-kelsey/su26-aai590-Group2`.
2. Settings -> Build and Deployment -> Root Directory = `website`.
3. Same page -> Ignored Build Step -> Custom:
   `git diff --quiet HEAD^ HEAD -- .`
   (skips builds for capstone commits that do not touch `website/`; the
   command runs inside the Root Directory).
4. Production Branch stays `main`.
5. Trigger a deploy (Deploys -> Redeploy, or merge any website change) and
   verify https://www.venue-economics.com serves it.

## Step 3: retire the mirror

1. Archive the GitHub repo `Jungleislander/venue-economics` (Settings ->
   Archive). Its git history stays browsable.
2. Delete `website/scripts/sync-from-team-repo.sh` from this repo and remove
   the interim-flow note in `website/CLAUDE.md`.
3. Local cleanup on Steve's machine: `~/Projects/hyperfocus/venue-economics`
   can be deleted or kept as an archive; the working copy is this repo.

## Notes

- Env vars: none are required while the model runs in simulated preview. When
  the live endpoint ships, set `AWS_REGION` / `AWS_ACCESS_KEY_ID` /
  `AWS_SECRET_ACCESS_KEY` / `SAGEMAKER_ENDPOINT_ORACLE` in the Vercel project
  (see docs/PLUG-IN-ENDPOINT.md). The retired `SAGEMAKER_ENDPOINT` var can be
  deleted.
- After cutover, every team-repo branch that touches `website/` gets a Vercel
  preview deployment automatically; website changes follow the normal
  branch-and-PR flow.
