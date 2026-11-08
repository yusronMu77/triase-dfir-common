"""Jalankan layanan: `astria-{{svc}}` atau `python -m astria_{{pkg}}`."""

from astria_common.app import run

from astria_{{pkg}}.main import build_app
from astria_{{pkg}}.settings import Settings


def main() -> None:
    settings = Settings()
    run(build_app(settings), settings)


if __name__ == "__main__":
    main()
