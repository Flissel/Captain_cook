"""Schema migration: add agent_registry and agent_improvements tables.

Run this if you have an existing minibook.db that predates the Agent Factory feature.
It is safe to run multiple times (uses CREATE TABLE IF NOT EXISTS).

Usage:
    cd minibook
    python scripts/migrate_schema.py [path/to/minibook.db]
"""
import sqlite3
import sys
import os

DB_PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(__file__), "..", "minibook.db"
)

MIGRATIONS = [
    # agent_registry table
    """
    CREATE TABLE IF NOT EXISTS agent_registry (
        id TEXT PRIMARY KEY,
        agent_id TEXT REFERENCES agents(id),
        team_key TEXT NOT NULL,
        run_id TEXT NOT NULL,
        capabilities TEXT NOT NULL DEFAULT '[]',
        mcp_servers TEXT NOT NULL DEFAULT '[]',
        tools_py_path TEXT,
        output_dir TEXT,
        eval_score INTEGER NOT NULL DEFAULT 0,
        eval_reason TEXT NOT NULL DEFAULT '',
        todo_status TEXT NOT NULL DEFAULT 'pending',
        status TEXT NOT NULL DEFAULT 'candidate',
        community_project_id TEXT REFERENCES projects(id),
        created_at DATETIME,
        updated_at DATETIME
    )
    """,
    # agent_improvements table
    """
    CREATE TABLE IF NOT EXISTS agent_improvements (
        id TEXT PRIMARY KEY,
        registry_id TEXT NOT NULL REFERENCES agent_registry(id),
        tool_name TEXT NOT NULL,
        improvement_type TEXT NOT NULL DEFAULT 'tool_impl',
        description TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'proposed',
        eval_score_before INTEGER NOT NULL DEFAULT 0,
        eval_score_after INTEGER NOT NULL DEFAULT 0,
        created_at DATETIME
    )
    """,
    # Add last_seen to agents if missing
    "ALTER TABLE agents ADD COLUMN last_seen DATETIME",
    # Add primary_lead_agent_id to projects if missing
    "ALTER TABLE projects ADD COLUMN primary_lead_agent_id TEXT REFERENCES agents(id)",
    # Add role_descriptions to projects if missing
    "ALTER TABLE projects ADD COLUMN role_descriptions TEXT DEFAULT '{}'",
    # Add pin_order to posts if missing
    "ALTER TABLE posts ADD COLUMN pin_order INTEGER",
    # Add github_ref to posts if missing
    "ALTER TABLE posts ADD COLUMN github_ref TEXT",
    # Captain projection idempotency/version fence
    """
    CREATE TABLE IF NOT EXISTS projection_post_fences (
        subject_key TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects(id),
        post_id TEXT NOT NULL UNIQUE REFERENCES posts(id),
        event_id TEXT NOT NULL UNIQUE,
        subject_version INTEGER NOT NULL,
        source_fingerprint TEXT NOT NULL,
        updated_at DATETIME
    )
    """,
    # Captain v2 event-specific post identity
    """
    CREATE TABLE IF NOT EXISTS projection_event_posts (
        event_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects(id),
        post_id TEXT NOT NULL UNIQUE REFERENCES posts(id),
        subject_key TEXT NOT NULL,
        subject_version INTEGER NOT NULL,
        source_fingerprint TEXT NOT NULL,
        created_at DATETIME
    )
    """,
    # Captain v2 monotonic subject head, deliberately separate from event posts
    """
    CREATE TABLE IF NOT EXISTS projection_subject_heads_v2 (
        subject_key TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects(id),
        subject_version INTEGER NOT NULL,
        event_id TEXT NOT NULL,
        source_fingerprint TEXT NOT NULL,
        updated_at DATETIME
    )
    """,
]

def run():
    print(f"Migrating: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("BEGIN")
        for sql in MIGRATIONS:
            stmt = sql.strip()
            try:
                conn.execute(stmt)
                first_line = stmt.splitlines()[0][:60]
                print(f"  OK: {first_line}")
            except sqlite3.OperationalError as error:
                message = str(error).lower()
                if "duplicate column" in message or "already exists" in message:
                    continue
                raise
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    print("Done.")

if __name__ == "__main__":
    run()
