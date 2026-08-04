#!/usr/bin/env node
// Fetch the Advan-derived POI search index (src/data/pois.json) from the
// capstone's private S3 bucket at build time. The index is derived from
// licensed data (Dewey/Advan student license) and is deliberately NOT
// committed to this public repo; cells.json and giants-schedule.json are
// committed, pois.json is fetched. Vercel runs this via `vercel-build`.
//
// Env (set in Vercel for Production + Preview):
//   POIS_AWS_ACCESS_KEY_ID / POIS_AWS_SECRET_ACCESS_KEY
//     IAM user `vercel-website-build`, s3:GetObject on website-data/* only.
//     Named POIS_* on purpose: the future live-endpoint runtime creds own
//     the bare AWS_* names (see docs/PLUG-IN-ENDPOINT.md).
//   POIS_S3_URI      default s3://aai-590-group2-capstone/website-data/pois.json
//   POIS_AWS_REGION  default us-east-2
//
// Local dev: if src/data/pois.json already exists (it does on team machines),
// this is a no-op. POIS_FORCE_FETCH=1 re-downloads.

import { S3Client, GetObjectCommand } from '@aws-sdk/client-s3';
import { existsSync, renameSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const target = join(dirname(fileURLToPath(import.meta.url)), '..', 'src', 'data', 'pois.json');

if (existsSync(target) && process.env.POIS_FORCE_FETCH !== '1') {
  console.log('fetch-pois: src/data/pois.json already present, skipping (POIS_FORCE_FETCH=1 to refetch)');
  process.exit(0);
}

const uri = process.env.POIS_S3_URI ?? 's3://aai-590-group2-capstone/website-data/pois.json';
const m = uri.match(/^s3:\/\/([^/]+)\/(.+)$/);
if (!m) {
  console.error(`fetch-pois: POIS_S3_URI is not a valid s3:// URI: ${uri}`);
  process.exit(1);
}
const [, Bucket, Key] = m;

const accessKeyId = process.env.POIS_AWS_ACCESS_KEY_ID;
const secretAccessKey = process.env.POIS_AWS_SECRET_ACCESS_KEY;
if (!accessKeyId || !secretAccessKey) {
  console.error(
    'fetch-pois: src/data/pois.json is missing and POIS_AWS_ACCESS_KEY_ID / ' +
      'POIS_AWS_SECRET_ACCESS_KEY are unset. /api/places cannot work without ' +
      'the index, so the build fails here rather than shipping broken search.',
  );
  process.exit(1);
}

const s3 = new S3Client({
  region: process.env.POIS_AWS_REGION ?? 'us-east-2',
  credentials: { accessKeyId, secretAccessKey },
});

try {
  const res = await s3.send(new GetObjectCommand({ Bucket, Key }));
  const body = await res.Body.transformToString();
  const parsed = JSON.parse(body);
  const count = parsed?.pois?.length ?? 0;
  if (count < 10_000 || parsed.meta?.count !== count) {
    console.error(`fetch-pois: sanity check failed (pois=${count}, meta.count=${parsed?.meta?.count})`);
    process.exit(1);
  }
  const tmp = `${target}.tmp`;
  writeFileSync(tmp, body);
  renameSync(tmp, target);
  console.log(`fetch-pois: wrote ${count} POIs (${Math.round(body.length / 1024)} KiB) from s3://${Bucket}/${Key}`);
} catch (err) {
  console.error(`fetch-pois: failed to fetch s3://${Bucket}/${Key}: ${err.name}: ${err.message}`);
  process.exit(1);
}
