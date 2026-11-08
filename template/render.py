"""Render templat layanan Python ke repo layanan.

    python template/render.py ../ingest --service ingest
    python template/render.py ../ingest --service ingest --managed-only   # hanya perbarui berkas terkelola

Berkas terkelola (Dockerfile, .dockerignore, .github/workflows/ci.yml) selalu ditulis ulang.
Berkas kerangka (pyproject.toml, src/, tests/) hanya ditulis bila belum ada, atau bila pyproject.toml masih
kerangka awal (tanpa [tool.uv.sources]). Deskripsi diambil dari pyproject.toml yang ada.
Hanya pustaka standar: bisa dijalankan tanpa memasang apa pun.
"""

from __future__ import annotations

import argparse
import re
import tomllib
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent / "service"
MANAGED = {"Dockerfile", ".dockerignore", ".github/workflows/ci.yml"}
DEFAULT_TAG = "v0.1.0"


def render_text(text: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def existing_description(target: Path) -> str | None:
    path = target / "pyproject.toml"
    if not path.exists():
        return None
    return tomllib.loads(path.read_text(encoding="utf-8")).get("project", {}).get("description")


def is_skeleton_pyproject(target: Path) -> bool:
    path = target / "pyproject.toml"
    return not path.exists() or "[tool.uv.sources]" not in path.read_text(encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("target", type=Path)
    parser.add_argument("--service", required=True, help="nama layanan, mis. ingest")
    parser.add_argument("--description")
    parser.add_argument("--common-tag", default=DEFAULT_TAG)
    parser.add_argument("--contracts-tag", default=DEFAULT_TAG)
    parser.add_argument("--managed-only", action="store_true")
    args = parser.parse_args()

    if not re.fullmatch(r"[a-z][a-z0-9-]*", args.service):
        parser.error("nama layanan harus huruf kecil, angka, dan tanda hubung")
    target: Path = args.target.resolve()
    description = args.description or existing_description(target) or f"Layanan {args.service}"
    values = {
        "svc": args.service,
        "pkg": args.service.replace("-", "_"),
        "description": description.replace('"', "'"),
        "common_tag": args.common_tag,
        "contracts_tag": args.contracts_tag,
    }
    skeleton = is_skeleton_pyproject(target)

    for src in sorted(p for p in TEMPLATE.rglob("*") if p.is_file()):
        rel = src.relative_to(TEMPLATE).as_posix()
        dst = target / render_text(rel, values)
        managed = rel in MANAGED
        if args.managed_only and not managed:
            continue
        if not managed and dst.exists() and not (rel == "pyproject.toml" and skeleton):
            print(f"lewati  {dst.relative_to(target).as_posix()} (sudah ada)")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(render_text(src.read_text(encoding="utf-8"), values), encoding="utf-8", newline="\n")
        print(f"tulis   {dst.relative_to(target).as_posix()}")


if __name__ == "__main__":
    main()
