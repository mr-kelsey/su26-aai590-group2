"""Register, gate, and deploy the Oracle Park ripple endpoint. boto3 only.

Deliberately split into separate subcommands, because two of them cost money and
one of them is a human decision:

    upload     put model.tar.gz in S3
    register   create the model package        -> PendingManualApproval
    approve    THE GATE. a person, with a note.
    endpoint   create-or-update, wait for InService   (this is the one that bills)
    status     what exists right now
    teardown   delete the endpoint

The promotion gate is the same shape the 540 project used in this account
(eia-foodsvc-xgb, versions 1-3, PendingManualApproval then Approved), so the
capstone's MLOps story is continuous rather than reinvented. `endpoint` refuses
to run unless the package is Approved.

update_endpoint gives blue/green for free: SageMaker stands the new variant up,
health-checks it, and only then shifts traffic. Because model_fn raises on a
golden-fixture mismatch, a bad artifact fails that health check and traffic never
moves. That is the whole safety story in one sentence.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..settings import settings

REGION = "us-east-2"
ACCOUNT = "541974874359"
BUCKET = "aai-590-group2-capstone"
INSTANCE = "ml.m5.large"

# Two model families, one deploy path. Every subcommand takes --model and
# defaults to the GBM, so existing invocations keep their exact behavior.
FAMILIES = {
    "oracle-ripple": {
        "group": "eia-nowcast-oracle-ripple",
        "endpoint": "eia-nowcast-oracle-ripple-v1",
        "key": "eia-nowcast/models/oracle-ripple/model.tar.gz",
        "artifact_dir": "serve_artifacts",
        "tarball": "model.tar.gz",
        "group_desc": (
            "Oracle Park game-evening ripple. Tier 1 LightGBM counterfactual "
            "on log1p(person-hours) at 250m cell-hour grain, plus a "
            "canonical-ring difference-in-differences effect layer."),
    },
    "oracle-ripple-stgnn": {
        "group": "eia-nowcast-oracle-ripple-stgnn",
        "endpoint": "eia-nowcast-oracle-ripple-stgnn-v1",
        "key": "eia-nowcast/models/oracle-ripple-stgnn/model.tar.gz",
        "artifact_dir": "serve_artifacts_stgnn",
        "tarball": "model-stgnn.tar.gz",
        "group_desc": (
            "Oracle Park game-evening ripple, Tier 2 arm. Spatiotemporal graph "
            "network counterfactual (visitor-origin flow edges) precomputed to "
            "a cf grid, plus its own canonical-ring DiD effect layer derived "
            "from STGNN residuals. Same wire schema as the Tier 1 endpoint."),
    },
}

# module-level defaults kept for backward compatibility with existing imports
GROUP = FAMILIES["oracle-ripple"]["group"]
ENDPOINT = FAMILIES["oracle-ripple"]["endpoint"]
MODEL_KEY = FAMILIES["oracle-ripple"]["key"]

# Verified present in this account: AmazonSageMakerFullAccess plus a policy
# granting s3:GetObject on arn:aws:s3:::*, which is what lets it pull the
# artifact from a bucket with no "sagemaker" in the name. SageMakerFullAccess
# alone could not.
ROLE_ARN = ("arn:aws:iam::541974874359:role/service-role/"
            "AmazonSageMaker-ExecutionRole-20260516T162536")

# Resolved via sagemaker.image_uris and then PINNED. The ECR account 257758044811
# is read off the existing registered xgboost package in this account, not guessed.
IMAGE = "257758044811.dkr.ecr.us-east-2.amazonaws.com/sagemaker-scikit-learn:1.2-1-cpu-py3"

CONTAINER_ENV = {
    "SAGEMAKER_PROGRAM": "inference.py",
    "SAGEMAKER_SUBMIT_DIRECTORY": "/opt/ml/model/code",
    "SAGEMAKER_CONTAINER_LOG_LEVEL": "20",
    "SAGEMAKER_REGION": REGION,
    # each worker holds ~90 MB of arrays; ml.m5.large has 8 GB
    "SAGEMAKER_MODEL_SERVER_WORKERS": "2",
    "MMS_DEFAULT_RESPONSE_TIMEOUT": "120",
}

TAGS = [{"Key": "project", "Value": "aai590-capstone"},
        {"Key": "component", "Value": "oracle-ripple"}]


def _sm():
    import boto3

    return boto3.client("sagemaker", region_name=REGION)


def _fam(model: str) -> dict:
    if model not in FAMILIES:
        raise SystemExit(f"unknown model {model!r}; one of {sorted(FAMILIES)}")
    return FAMILIES[model]


def _manifest(model: str = "oracle-ripple") -> dict:
    p = settings.data_dir / _fam(model)["artifact_dir"] / "manifest.json"
    return json.loads(p.read_text())


def _meta_safe(d: dict) -> dict:
    """CustomerMetadataProperties accepts a narrow charset and 256 chars.

    The allowed set is letters, separators, numbers and `_.:/=+-@`, so a comma, a
    semicolon or a parenthesis fails the whole CreateModelPackage call with a
    validation error that names every key at once and is miserable to read. Strip
    rather than let a future edit rediscover that.
    """
    import re

    bad = re.compile(r"[^\w\s.:/=+\-@]", re.UNICODE)
    out = {}
    for k, v in d.items():
        s = bad.sub(" ", str(v)).strip()
        s = re.sub(r"\s+", " ", s)
        out[k] = s[:256]
    return out


# ---------------------------------------------------------------- upload


def upload(tarball: Path | None = None, model: str = "oracle-ripple") -> str:
    from .package import upload as _up

    fam = _fam(model)
    tarball = tarball or (settings.data_dir / "dist" / fam["tarball"])
    return _up(tarball, key=fam["key"])


# ---------------------------------------------------------------- register


def _metadata(model: str, man: dict, eff: dict) -> dict:
    """Per-family registry metadata. Shared limitations stay shared."""
    common = {
        "target": "person_hours",
        "measure": "visitor_hours",
        "evening_hours": "16-23",
        "training_window": "/".join(man["training_window"]),
        "serve_window": "/".join(man["serve_window"]),
        "observed_panel_through": man["observed_panel_through"],
        "effect_window": "/".join(eff["effect_window"]),
        "n_cells": str(man["n_cells"]),
        "featurespec_sha256": man["featurespec_sha256"][:16],
        "limitation_core_ring": "0-250m ring is one 250m cell with 27 POIs",
        "limitation_projected": (
            f"dates after {man['observed_panel_through']} are forward-projected"),
        "limitation_window": "effects are evening-only 16-23 and must not be "
                             "applied to a whole day",
    }
    core = next(b for b in eff["bands"] if b["id"] == "b1")
    common["core_ring_lift_pct"] = f"{core['lift_pct']:.2f}"
    suppressed = [b["id"] for b in eff["bands"] if not b.get("significant", True)]
    common["limitation_suppressed_bands"] = (
        "bands not distinguishable from zero ship as zero: "
        + (",".join(suppressed) or "none"))
    if model == "oracle-ripple":
        common.update({
            "objective": "l2_on_log1p",
            "held_out_test_mae": "0.9185",
            "held_out_test_r2": "0.7568",
            "served_variant": "full_control_all_splits",
        })
    else:
        met = man.get("checkpoint_metrics", {})
        common.update({
            "objective": "masked_l2_on_log1p",
            "edge_key": man.get("edge_key", ""),
            "cf_source": man.get("cf_source", ""),
            "held_out_test_mae": f"{met.get('test_mae', float('nan')):.4f}",
            "served_variant": man.get("served_variant", "train_split_checkpoint"),
            "t_index_policy": man.get("t_index_policy", "")[:250],
            "limitation_basis": (
                "Tier 2 is scored on overlapping-window predictions; not "
                "directly comparable with the Tier 1 MAE until compare_tiers "
                "runs both through predict_grid"),
        })
    return common


def register(model_data_url: str | None = None, notes: str = "",
             model: str = "oracle-ripple") -> str:
    sm = _sm()
    fam = _fam(model)
    man = _manifest(model)
    url = model_data_url or f"s3://{BUCKET}/{fam['key']}"

    try:
        sm.create_model_package_group(
            ModelPackageGroupName=fam["group"],
            ModelPackageGroupDescription=fam["group_desc"],
        )
        print(f"  created model package group {fam['group']}", flush=True)
    except Exception as e:
        # SageMaker raises ValidationException with "already exists" here, NOT
        # ResourceInUse, so catching the typed exception silently does nothing.
        if "already exists" not in str(e):
            raise

    eff = json.loads(
        (settings.data_dir / fam["artifact_dir"] / "effects.json").read_text())

    resp = sm.create_model_package(
        ModelPackageGroupName=fam["group"],
        ModelPackageDescription=(notes or fam["group_desc"][:160]),
        InferenceSpecification={
            "Containers": [{
                "Image": IMAGE,
                "ModelDataUrl": url,
                "Environment": CONTAINER_ENV,
            }],
            "SupportedContentTypes": ["application/json"],
            "SupportedResponseMIMETypes": ["application/json"],
            "SupportedRealtimeInferenceInstanceTypes": [INSTANCE],
            "SupportedTransformInstanceTypes": [INSTANCE],
        },
        ModelApprovalStatus="PendingManualApproval",
        CustomerMetadataProperties=_meta_safe(_metadata(model, man, eff)),
    )
    arn = resp["ModelPackageArn"]
    print(f"  registered {arn}\n  status: PendingManualApproval", flush=True)
    return arn


def latest_package(status: str | None = None, model: str = "oracle-ripple") -> str:
    sm = _sm()
    group = _fam(model)["group"]
    kw = {"ModelPackageGroupName": group, "SortBy": "CreationTime",
          "SortOrder": "Descending", "MaxResults": 10}
    if status:
        kw["ModelApprovalStatus"] = status
    got = sm.list_model_packages(**kw)["ModelPackageSummaryList"]
    if not got:
        raise RuntimeError(f"no model packages in {group}"
                           + (f" with status {status}" if status else ""))
    return got[0]["ModelPackageArn"]


def approve(arn: str | None = None, note: str = "",
            model: str = "oracle-ripple") -> None:
    """The gate. A person runs this, with a reason, as its own deliberate step."""
    if not note:
        raise SystemExit("--note is required: the gate is a decision, so record it")
    sm = _sm()
    arn = arn or latest_package("PendingManualApproval", model)
    sm.update_model_package(ModelPackageArn=arn, ModelApprovalStatus="Approved",
                            ApprovalDescription=note)
    print(f"  approved {arn}\n  note: {note}", flush=True)


# ---------------------------------------------------------------- deploy


def endpoint(arn: str | None = None, wait: bool = True,
             model: str = "oracle-ripple") -> str:
    import datetime as dt

    sm = _sm()
    fam = _fam(model)
    ep = fam["endpoint"]
    arn = arn or latest_package("Approved", model)
    d = sm.describe_model_package(ModelPackageName=arn)
    if d["ModelApprovalStatus"] != "Approved":
        raise RuntimeError(f"promotion gate not passed: {d['ModelApprovalStatus']}")

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    model_name = f"eia-{model}-{stamp}"
    sm.create_model(
        ModelName=model_name,
        PrimaryContainer={"ModelPackageName": arn},
        ExecutionRoleArn=ROLE_ARN,
        Tags=TAGS,
    )

    cfg = f"{ep}-cfg-{stamp}"
    sm.create_endpoint_config(
        EndpointConfigName=cfg,
        ProductionVariants=[{
            "VariantName": "AllTraffic",
            "ModelName": model_name,
            "InitialInstanceCount": 1,
            "InstanceType": INSTANCE,
            "InitialVariantWeight": 1.0,
            # the container pip-installs the vendored wheel and then verifies the
            # golden fixture before it answers a ping; give it room
            "ContainerStartupHealthCheckTimeoutInSeconds": 600,
        }],
        DataCaptureConfig={
            "EnableCapture": True,
            "InitialSamplingPercentage": 100,
            "DestinationS3Uri": f"s3://{BUCKET}/eia-nowcast/capture/{ep}/",
            "CaptureOptions": [{"CaptureMode": "Input"}, {"CaptureMode": "Output"}],
        },
        Tags=TAGS,
    )

    existing = {e["EndpointName"]
                for e in sm.list_endpoints(NameContains=ep)["Endpoints"]}
    if ep in existing:
        sm.update_endpoint(EndpointName=ep, EndpointConfigName=cfg)
        print(f"  updating {ep} -> {cfg} (blue/green)", flush=True)
    else:
        sm.create_endpoint(EndpointName=ep, EndpointConfigName=cfg, Tags=TAGS)
        print(f"  creating {ep} -> {cfg}", flush=True)

    if wait:
        print("  waiting for InService (up to 20 min) ...", flush=True)
        try:
            sm.get_waiter("endpoint_in_service").wait(
                EndpointName=ep, WaiterConfig={"Delay": 30, "MaxAttempts": 40})
        except Exception:
            desc = sm.describe_endpoint(EndpointName=ep)
            raise RuntimeError(
                f"endpoint is {desc['EndpointStatus']}: "
                f"{desc.get('FailureReason', 'no reason given')}"
            )
    ep_arn = f"arn:aws:sagemaker:{REGION}:{ACCOUNT}:endpoint/{ep}"
    print(f"  InService: {ep_arn}", flush=True)
    return ep_arn


def status() -> None:
    sm = _sm()
    for model, fam in FAMILIES.items():
        try:
            pkgs = sm.list_model_packages(
                ModelPackageGroupName=fam["group"], SortBy="CreationTime",
                SortOrder="Descending", MaxResults=5)["ModelPackageSummaryList"]
        except Exception:
            pkgs = []
        print(f"model packages ({fam['group']}):")
        for p in pkgs:
            print(f"  v{p['ModelPackageVersion']:<3} {p['ModelApprovalStatus']:<22} "
                  f"{p['CreationTime']:%Y-%m-%d %H:%M}")
        if not pkgs:
            print("  (none)")
    print("endpoints:")
    for e in sm.list_endpoints()["Endpoints"]:
        print(f"  {e['EndpointName']:<40} {e['EndpointStatus']}")


def teardown(model: str = "oracle-ripple") -> None:
    sm = _sm()
    ep = _fam(model)["endpoint"]
    sm.delete_endpoint(EndpointName=ep)
    print(f"  deleted endpoint {ep} (config and model kept)", flush=True)


def grant_invoke(user: str = "venue-economics-invoke",
                 model: str = "oracle-ripple") -> None:
    """One action, one resource ARN per policy. No wildcard, no Describe, no S3."""
    import boto3

    ep = _fam(model)["endpoint"]
    doc = {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "InvokeOracleRippleEndpointOnly",
            "Effect": "Allow",
            "Action": "sagemaker:InvokeEndpoint",
            "Resource": f"arn:aws:sagemaker:{REGION}:{ACCOUNT}:endpoint/{ep}",
        }],
    }
    boto3.client("iam").put_user_policy(
        UserName=user,
        PolicyName=f"invoke-{ep}",
        PolicyDocument=json.dumps(doc),
    )
    print(f"  granted {user} sagemaker:InvokeEndpoint on {ep} only", flush=True)


def main(argv=None) -> None:
    import argparse

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--model", default="oracle-ripple",
                        choices=sorted(FAMILIES))

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("upload", parents=[common])
    r = sub.add_parser("register", parents=[common])
    r.add_argument("--notes", default="")
    a = sub.add_parser("approve", parents=[common])
    a.add_argument("--note", default="")
    a.add_argument("--arn", default=None)
    e = sub.add_parser("endpoint", parents=[common])
    e.add_argument("--arn", default=None)
    e.add_argument("--no-wait", action="store_true")
    sub.add_parser("status")
    sub.add_parser("teardown", parents=[common])
    g = sub.add_parser("grant-invoke", parents=[common])
    g.add_argument("--user", default="venue-economics-invoke")
    n = ap.parse_args(argv)

    if n.cmd == "upload":
        upload(model=n.model)
    elif n.cmd == "register":
        register(notes=n.notes, model=n.model)
    elif n.cmd == "approve":
        approve(n.arn, n.note, model=n.model)
    elif n.cmd == "endpoint":
        endpoint(n.arn, wait=not n.no_wait, model=n.model)
    elif n.cmd == "status":
        status()
    elif n.cmd == "teardown":
        teardown(model=n.model)
    elif n.cmd == "grant-invoke":
        grant_invoke(n.user, model=n.model)


if __name__ == "__main__":  # pragma: no cover
    main()
