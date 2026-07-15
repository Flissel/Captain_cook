from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import sys

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from agenten.delivery.minibook_client import MinibookClient
from agenten.delivery.projector import MinibookProjector


def main() -> int:
    parser = argparse.ArgumentParser(description="Post or update the Captain delivery plan")
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--project", required=True)
    args = parser.parse_args()

    content = args.plan.read_text(encoding="utf-8")
    first_line = next((line for line in content.splitlines() if line.startswith("# ")), None)
    if first_line is None:
        raise SystemExit("plan has no level-one heading")
    title = first_line.removeprefix("# ").strip()
    base_url = os.environ.get("MINIBOOK_URL", "http://127.0.0.1:3456")
    client = MinibookClient.from_hermes_profile(base_url=base_url)
    projector = MinibookProjector(client)
    project = projector.ensure_project(args.project, "Durable agent delivery team")
    post = projector.upsert_plan(project["id"], title, content)
    readback = client.get_post(post["id"])["content"]
    expected_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    actual_hash = hashlib.sha256(readback.encode("utf-8")).hexdigest()
    if actual_hash != expected_hash:
        raise SystemExit("Minibook read-back hash differs from local plan")
    print(f"project_id={project['id']}")
    print(f"post_id={post['id']}")
    print(f"sha256={actual_hash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
