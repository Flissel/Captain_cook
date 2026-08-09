from pathlib import Path
import sys

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from agenten.provider_proxy.entrypoint import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
