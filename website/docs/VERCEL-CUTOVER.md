# Vercel cutover: deploy the website from THIS repo

Goal: Vercel builds `website/` from `mr-kelsey/su26-aai590-Group2` directly, the
domain stays put, and the old personal mirror repo retires. Until every step
below is done, production keeps deploying from the mirror
(`Jungleislander/venue-economics`) via `scripts/sync-from-team-repo.sh`.

## STATUS 2026-08-04: blocked by a Vercel platform rule. Team decision needed.

Attempted 2026-08-04, after Jonno installed the GitHub App (step 1, done).
The `mr-kelsey` namespace never appears in the repo picker of the Vercel
project, and Vercel's docs say why
(<https://vercel.com/docs/git/vercel-for-github>, "Personal account
repositories"):

> To import or connect a GitHub repository owned by a personal account, you
> must be the repository Owner. [...] A Collaborator on a personal repository
> cannot create new Vercel projects from that repository or connect it to
> existing projects.

`mr-kelsey` is a personal GitHub account. The Vercel project
(`venue-economics`, with the domain attached) lives under Steve's Vercel
account, and Steve's GitHub login is a collaborator on this repo, not its
owner. So the direct connect is impossible regardless of the App install.
The App install was still worth doing: both options below use it.

The interim mirror flow keeps working in the meantime; nothing is broken.

## Options (approving one of these in review = picking it)

### Option A (recommended): move the repo into a GitHub organization

Vercel lets org MEMBERS with repo access connect org repos. One-time, about
10 minutes, and afterwards the original step 2 works verbatim:

1. Jonno creates a free GitHub org (e.g. `onegiantleap`) and transfers
   `su26-aai590-Group2` into it (repo Settings -> Transfer ownership).
   GitHub redirects the old URLs and existing clones keep working.
2. Jonno adds Steve and Luke as org members with access to the repo
   (outside collaborators are NOT enough per Vercel's docs).
3. Jonno installs the Vercel GitHub App on the org, selecting the repo.
4. Steve runs step 2 below unchanged.

Check first: if the course requires the repo to stay under `mr-kelsey`,
use Option B instead.

### Option B: deploy from GitHub Actions in this repo (no repo move)

Vercel's documented fallback (same docs page, "Using GitHub Actions"):
workflows in this repo run `vercel pull` / `vercel build` / `vercel deploy
--prebuilt` against Steve's Vercel project, authenticated by a token.

1. Steve creates a Vercel access token for his account.
2. Jonno adds it to this repo as the `VERCEL_TOKEN` Actions secret (repo
   Settings -> Secrets; only the owner can).
3. Add two workflows under `.github/workflows/`: preview deploys on branch
   pushes touching `website/`, production deploy on pushes to `main`.

Trade-offs: token handling (the token can deploy Steve's projects), no
native PR comments or commit statuses from Vercel, and we maintain the
workflows ourselves.

### Not viable

Moving the Vercel project or domain under Jonno's Vercel account, or a paid
shared Vercel team, would also work but has the most churn (domain and
project settings move). Listed only for completeness.

## Step 1: Jonno (repo owner), one-time GitHub App install. DONE 2026-08-04.

1. Open https://github.com/apps/vercel and click Configure.
2. Pick the `mr-kelsey` account.
3. Choose "Only select repositories" and select `su26-aai590-Group2`.
4. Save. Nothing else; no Vercel account needed.

## Step 2: Steve, in the Vercel dashboard (project `venue-economics`)

Works only after Option A (or is replaced by workflows under Option B).

1. Settings -> Git -> Disconnect the current repo
   (`Jungleislander/venue-economics`), then Connect Git Repository ->
   the org's `su26-aai590-Group2`.
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
  branch-and-PR flow. (Under Option B this comes from the workflows instead.)
