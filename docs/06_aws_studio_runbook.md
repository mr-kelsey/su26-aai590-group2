# 06 — AWS SageMaker Studio Runbook (round 1: land gold to S3)

Runs the gold layer from inside a Studio instance using its execution role. The
boto3-from-laptop path is not reliable here (see 05 §6 / CLAUDE.md) — run these steps
**inside Studio**.

## 0. One-time seed (irreplaceable BART artifacts — dead host, D11)
Upload these local files to s3://aai-590-group2-capstone/eia-nowcast/… (S3 console UI or
from Studio). They cannot be regenerated:

| local file | S3 key (under eia-nowcast/) |
|---|---|
| data/raw/bart/od_2024.parquet | raw/bart/od_2024.parquet |
| data/reference/bart_stations.parquet | reference/bart_stations.parquet |
| data/reference/giants_home_2024.parquet | reference/giants_home_2024.parquet |
| data/gold/bart_attendance_calib_2024.parquet | gold/bart_attendance_calib_2024.parquet |
| data/transform/sf_daily_arrivals_2024.parquet | gold/sf_daily_arrivals_2024.parquet |
| data/transform/game_residuals_0_300m.parquet | gold/game_residuals_0_300m.parquet |
| data/gold/sf_food_services_daily_2024.parquet | gold/sf_food_services_daily_2024.parquet |

## 1. Clone + install
    git clone https://github.com/Giant-Leap-ai/eia-nowcast-pipeline.git
    cd eia-nowcast-pipeline
    pip install -e .          # Studio base image is pip/conda; uv optional

## 2. Configure (env, not committed)
    export S3_BUCKET=aai-590-group2-capstone
    export S3_PREFIX=eia-nowcast
    export AWS_REGION=<instance region>

## 3. Write smoke-test (gate — stop here if it fails)
    python -c "import boto3,os; boto3.client('s3').put_object(Bucket=os.environ['S3_BUCKET'], Key=os.environ['S3_PREFIX']+'/_smoketest', Body=b'ok'); print('S3 write OK')"

## 4. Run the runner
    python -m eia_pipeline.run_all --to-s3 --seasons 2024

## 5. Verify (list + read-back in place)
    python -c "from eia_pipeline.io import list_s3; [print(o['key']) for o in list_s3('eia-nowcast/gold/')]"
    python - <<'PY'
import os, duckdb
b = os.environ["S3_BUCKET"]
con = duckdb.connect()
con.sql("INSTALL aws; LOAD aws; INSTALL httpfs; LOAD httpfs;")
con.sql("CREATE SECRET (TYPE S3, PROVIDER CREDENTIAL_CHAIN);")
print(con.sql(f"SELECT count(*) FROM 's3://{b}/eia-nowcast/gold/cdtfa_food_services.parquet'").pl())
PY
    # Note: CREATE SECRET is needed because DuckDB httpfs doesn't inherit the instance-role credential chain automatically (unlike boto3's list_s3 above).
