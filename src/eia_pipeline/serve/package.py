"""Assemble model.tar.gz and put it on S3.

Tarball layout, which is the SageMaker convention plus our own artifacts dir:

    model.txt              booster at the root, where inference.py looks for it
    artifacts/             everything artifacts.py wrote
    code/inference.py      the handler
    code/featurespec.py    BYTE-IDENTICAL copy of serve/featurespec.py
    code/requirements.txt  offline install of the vendored wheel
    code/wheels/           lightgbm-4.5.0-py3-none-manylinux...whl

TWO COPIES OF featurespec.py, ONE SOURCE. The training side imports
`eia_pipeline.serve.featurespec`; the container imports the copy in code/. They
must be the same file or the golden fixture is meaningless, so the copy is made
here, its sha256 goes in the manifest, and both model_fn and a unit test compare
against it. Editing one without rebuilding fails the container at startup.

THE WHEEL IS VENDORED rather than installed from PyPI. LightGBM publishes a
`py3-none-manylinux` wheel with the shared library bundled, so one file works on
whatever Python 3 the container ships and container start never depends on PyPI
being reachable. `--no-index` in requirements.txt makes that a guarantee instead
of a preference.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
from pathlib import Path

from ..settings import settings
from . import featurespec as fs

# NOTE: handler/inference.py is deliberately NOT imported here. It does a flat
# `import featurespec`, which is right inside the container (SageMaker puts code/
# on sys.path) and wrong from the package. build_tarball compile-checks it
# instead, and smoke.py exercises it for real against an extracted tarball.

LIGHTGBM_VERSION = "4.5.0"
WHEEL = f"lightgbm-{LIGHTGBM_VERSION}-py3-none-manylinux_2_28_x86_64.whl"

REQUIREMENTS = f"""--no-index
--find-links ./wheels
lightgbm=={LIGHTGBM_VERSION}
"""


def _wheel_url() -> str:
    """Resolve the wheel's download URL from the PyPI JSON API.

    files.pythonhosted.org paths carry a content hash, so they cannot be
    constructed from the filename. Ask rather than guess.
    """
    import urllib.request

    api = f"https://pypi.org/pypi/lightgbm/{LIGHTGBM_VERSION}/json"
    with urllib.request.urlopen(api) as r:
        meta = json.loads(r.read())
    for f in meta["urls"]:
        if f["filename"] == WHEEL:
            return f["url"]
    raise RuntimeError(f"{WHEEL} not published for lightgbm {LIGHTGBM_VERSION}")


def handler_dir() -> Path:
    return Path(__file__).parent / "handler"


def fetch_wheel(dest: Path | None = None) -> Path:
    """Download the manylinux LightGBM wheel if it is not already vendored."""
    import urllib.request

    dest = dest or (handler_dir() / "wheels" / WHEEL)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 1_000_000:
        return dest
    print(f"  fetching {WHEEL} ...", flush=True)
    urllib.request.urlretrieve(_wheel_url(), dest)
    if dest.stat().st_size < 1_000_000:
        dest.unlink()
        raise RuntimeError(f"{WHEEL} came back too small; check the URL")
    print(f"  vendored {dest.name} ({dest.stat().st_size / 1e6:.1f} MB)", flush=True)
    return dest


def build_tarball(artifact_dir: Path | None = None, dest: Path | None = None,
                  model_version: str | None = None) -> Path:
    art = Path(artifact_dir or (settings.data_dir / "serve_artifacts"))
    out = Path(dest or (settings.data_dir / "dist" / "model.tar.gz"))
    out.parent.mkdir(parents=True, exist_ok=True)
    stage = out.parent / "_stage"
    if stage.exists():
        shutil.rmtree(stage)
    (stage / "artifacts").mkdir(parents=True)
    (stage / "code" / "wheels").mkdir(parents=True)

    man = json.loads((art / "manifest.json").read_text())
    if model_version:
        man["model_version"] = model_version
    cf_source = man.get("cf_source", "gbm")
    if cf_source == "gbm":
        man["lightgbm_version"] = LIGHTGBM_VERSION
    elif not man.get("model_version"):
        # the GBM fallback in inference.py names itself "gbm-..."; a grid
        # tarball without an explicit version would masquerade as the GBM
        raise RuntimeError("cf_source=grid tarballs require --model-version")

    for p in sorted(art.glob("*")):
        if p.name == "model.txt":
            shutil.copy2(p, stage / "model.txt")
        elif p.name != "manifest.json":
            shutil.copy2(p, stage / "artifacts" / p.name)

    src = Path(fs.__file__)
    shutil.copy2(src, stage / "code" / "featurespec.py")
    copied_sha = hashlib.sha256((stage / "code" / "featurespec.py").read_bytes()).hexdigest()
    if copied_sha != man["featurespec_sha256"]:
        raise RuntimeError(
            "featurespec.py changed since the artifacts were built "
            f"({copied_sha[:12]} vs {man['featurespec_sha256'][:12]}); "
            "re-run artifacts.build() before packaging"
        )
    handler = handler_dir() / "inference.py"
    compile(handler.read_text(), str(handler), "exec")   # syntax gate, not an import
    shutil.copy2(handler, stage / "code" / "inference.py")
    if cf_source == "gbm":
        # only the booster path needs lightgbm; a grid tarball ships no
        # requirements.txt at all, so the container skips pip entirely
        (stage / "code" / "requirements.txt").write_text(REQUIREMENTS)
        shutil.copy2(fetch_wheel(), stage / "code" / "wheels" / WHEEL)
    else:
        (stage / "code" / "wheels").rmdir()

    (stage / "artifacts" / "manifest.json").write_text(json.dumps(man, indent=2) + "\n")

    with tarfile.open(out, "w:gz") as tar:
        for p in sorted(stage.rglob("*")):
            if p.is_file():
                tar.add(p, arcname=str(p.relative_to(stage)))
    shutil.rmtree(stage)
    print(f"  model.tar.gz: {out.stat().st_size / 1e6:.1f} MB -> {out}", flush=True)
    return out


def upload(tarball: Path | None = None, key: str | None = None) -> str:
    from ..io import s3_client

    tarball = Path(tarball or (settings.data_dir / "dist" / "model.tar.gz"))
    if not settings.s3_bucket:
        raise RuntimeError("S3_BUCKET not set")
    key = key or f"{settings.s3_prefix}/models/oracle-ripple/model.tar.gz"
    s3_client().upload_file(str(tarball), settings.s3_bucket, key)
    uri = f"s3://{settings.s3_bucket}/{key}"
    print(f"  uploaded -> {uri}", flush=True)
    return uri


if __name__ == "__main__":  # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--upload", action="store_true")
    ap.add_argument("--model-version", default=None)
    ap.add_argument("--artifact-dir", default=None,
                    help="e.g. data/serve_artifacts_stgnn")
    ap.add_argument("--dest", default=None,
                    help="e.g. data/dist/model-stgnn.tar.gz")
    a = ap.parse_args()
    t = build_tarball(artifact_dir=a.artifact_dir, dest=a.dest,
                      model_version=a.model_version)
    if a.upload:
        upload(t)
