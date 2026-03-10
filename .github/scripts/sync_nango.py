#!/usr/bin/env python3
"""
sync_nango.py — Auto-register new MCP servers from awesome-mcp-servers README into Nango.

How it works:
1. Parses README.md to extract all server entries (name, GitHub URL, description, category, tags)
2. Fetches all existing Nango integrations under the "mcp-" prefix
3. Diffs: finds servers in README that don't have a Nango integration yet
4. Creates a Nango integration stub for each new server (provider: unauthenticated)
5. Creates a connection with full metadata (URL, description, category, language, scope)
6. Outputs a summary of what was added

Run:
    NANGO_SECRET_KEY=<key> python3 sync_nango.py [--dry-run] [--readme README.md]
"""

import os
import re
import sys
import json
import time
import argparse
import requests
from urllib.parse import urlparse

# ── Config ─────────────────────────────────────────────────────────────────────
NANGO_SECRET = os.environ.get("NANGO_SECRET_KEY", "4f1f3800-a006-4f95-aa6a-e8c3d66cf0df")
NANGO_API    = "https://api.nango.dev"
README_PATH  = os.environ.get("README_PATH", "README.md")
DRY_RUN      = os.environ.get("DRY_RUN", "false").lower() == "true"

# Emoji → metadata mappings
LANG_MAP = {
    "🐍": "python",
    "📇": "typescript",
    "🏎️": "go",
    "🦀": "rust",
    "#️⃣": "csharp",
    "☕": "java",
    "🌊": "c_cpp",
    "💎": "ruby",
}
SCOPE_MAP = {
    "☁️": "cloud",
    "🏠": "local",
    "📟": "embedded",
}
OS_MAP = {
    "🍎": "macos",
    "🪟": "windows",
    "🐧": "linux",
}

# ── Nango helpers ───────────────────────────────────────────────────────────────
def nango_headers():
    return {
        "Authorization": f"Bearer {NANGO_SECRET}",
        "Content-Type": "application/json",
    }

def nango_get_integrations():
    """Return set of existing integration unique_keys."""
    r = requests.get(f"{NANGO_API}/integrations", headers=nango_headers(), timeout=15)
    r.raise_for_status()
    data = r.json()
    integrations = data.get("data", [])
    return {c["unique_key"] for c in integrations}

def nango_create_integration(integration_id: str, display_name: str) -> bool:
    payload = {
        "provider": "unauthenticated",
        "unique_key": integration_id,
        "display_name": display_name,
    }
    r = requests.post(f"{NANGO_API}/integrations", headers=nango_headers(), json=payload, timeout=15)
    if r.status_code in (200, 201):
        return True
    print(f"    ⚠ Integration create failed [{r.status_code}]: {r.text[:200]}")
    return False

def nango_create_connection(integration_id: str, connection_id: str, metadata: dict) -> bool:
    payload = {
        "provider_config_key": integration_id,
        "connection_id": connection_id,
        "metadata": metadata,
    }
    r = requests.post(f"{NANGO_API}/connection", headers=nango_headers(), json=payload, timeout=15)
    if r.status_code in (200, 201):
        return True
    print(f"    ⚠ Connection create failed [{r.status_code}]: {r.text[:200]}")
    return False

# ── README parser ───────────────────────────────────────────────────────────────
ENTRY_RE = re.compile(
    r"^-\s+\[([^\]]+)\]\((https?://[^\)]+)\)"  # - [name](url)
    r"(?:\s+\[[^\]]*\]\([^\)]*\))*"             # optional extra links like [glama](...)
    r"([\s\S]*?)(?=\n-\s|\n#{1,3}\s|\Z)",       # rest of line until next entry or heading
    re.MULTILINE,
)
HEADING_RE = re.compile(r"^###\s+(.+)$", re.MULTILINE)

def slugify(text: str) -> str:
    """Convert text to a valid Nango integration ID."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    return text[:80]  # Nango max key length

def extract_tags(text: str) -> dict:
    langs, scopes, oses = [], [], []
    for emoji, lang in LANG_MAP.items():
        if emoji in text:
            langs.append(lang)
    for emoji, scope in SCOPE_MAP.items():
        if emoji in text:
            scopes.append(scope)
    for emoji, os_name in OS_MAP.items():
        if emoji in text:
            oses.append(os_name)
    return {"languages": langs, "scopes": scopes, "os": oses}

def parse_readme(path: str) -> list[dict]:
    """Parse README.md and return list of server dicts."""
    with open(path, encoding="utf-8") as f:
        content = f.read()

    servers = []
    current_category = "uncategorized"

    # Split by lines and track headings + entries
    lines = content.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]

        # Track category from ### headings
        h3 = re.match(r"^###\s+(.+)$", line)
        if h3:
            cat_raw = h3.group(1).strip()
            # Remove HTML anchor tags like <a name="..."></a>
            cat_raw = re.sub(r"<[^>]+>", "", cat_raw)
            # Remove markdown links like [text](url)
            cat_raw = re.sub(r"\[([^\]]+)\]\([^\)]*\)", r"\1", cat_raw)
            # Remove emoji (non-ASCII) and extra punctuation, keep alphanumeric + common chars
            cat_clean = re.sub(r"[^\w\s&/(),-]", "", cat_raw).strip()
            cat_clean = re.sub(r"\s+", " ", cat_clean).strip()
            current_category = cat_clean if cat_clean else "uncategorized"
            i += 1
            continue

        # Match server entry lines
        m = re.match(r"^-\s+\[([^\]]+)\]\((https?://[^\)]+)\)(.*)", line)
        if m:
            name = m.group(1).strip()
            url  = m.group(2).strip()
            rest = m.group(3)

            # Extract description (after " - ")
            desc_match = re.search(r"\s+-\s+(.+)", rest)
            description = desc_match.group(1).strip() if desc_match else ""
            # Clean up description (remove emoji)
            description = re.sub(r"[^\x00-\x7F]+", "", description).strip(" -")

            tags = extract_tags(rest)

            # Derive GitHub repo slug
            parsed = urlparse(url)
            repo_slug = parsed.path.strip("/")  # e.g. "owner/repo"

            # Build integration ID: "mcp-" + slugified repo
            integration_id = "mcp-" + slugify(repo_slug)

            servers.append({
                "name": name,
                "url": url,
                "repo": repo_slug,
                "integration_id": integration_id,
                "connection_id": f"{integration_id}-conn",
                "description": description[:500],
                "category": current_category,
                "languages": tags["languages"],
                "scopes": tags["scopes"],
                "os": tags["os"],
            })
        i += 1

    return servers

# ── Main ────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Parse only, don't write to Nango")
    parser.add_argument("--readme", default=README_PATH, help="Path to README.md")
    parser.add_argument("--limit", type=int, default=0, help="Max new servers to register (0=all)")
    args = parser.parse_args()

    dry_run = args.dry_run or DRY_RUN
    readme  = args.readme

    print(f"{'[DRY RUN] ' if dry_run else ''}Parsing {readme}...")
    servers = parse_readme(readme)
    print(f"  Found {len(servers):,} server entries in README")

    if dry_run:
        print("\nSample entries:")
        for s in servers[:5]:
            print(f"  [{s['category']}] {s['name']} → {s['integration_id']}")
            print(f"    URL: {s['url']}")
            print(f"    Tags: langs={s['languages']} scopes={s['scopes']}")
        return

    print("\nFetching existing Nango integrations...")
    existing = nango_get_integrations()
    mcp_existing = {k for k in existing if k.startswith("mcp-")}
    print(f"  {len(mcp_existing)} existing MCP integrations in Nango")

    # Find new servers
    new_servers = [s for s in servers if s["integration_id"] not in existing]
    print(f"  {len(new_servers)} new servers to register")

    if args.limit > 0:
        new_servers = new_servers[:args.limit]
        print(f"  (limited to {args.limit})")

    added, failed = 0, 0
    for s in new_servers:
        print(f"\n  + {s['name']} [{s['category']}]")
        print(f"    ID: {s['integration_id']}")

        # Create integration
        ok = nango_create_integration(
            integration_id=s["integration_id"],
            display_name=f"{s['name']} (MCP)",
        )
        if not ok:
            failed += 1
            continue
        time.sleep(0.2)  # rate limit

        # Create connection with full metadata
        metadata = {
            "source": "awesome-mcp-servers",
            "category": s["category"],
            "description": s["description"],
            "github_url": s["url"],
            "repo": s["repo"],
            "languages": s["languages"],
            "scopes": s["scopes"],
            "os": s["os"],
            "auto_registered": True,
            "registered_at": time.strftime("%Y-%m-%d"),
        }
        ok2 = nango_create_connection(
            integration_id=s["integration_id"],
            connection_id=s["connection_id"],
            metadata=metadata,
        )
        if ok2:
            print(f"    ✓ Registered")
            added += 1
        else:
            failed += 1
        time.sleep(0.2)

    print(f"\n{'='*50}")
    print(f"Done. Added: {added} | Failed: {failed} | Total in README: {len(servers)}")
    print(f"{'='*50}")

    # Write summary for GitHub Actions output
    summary = {
        "total_in_readme": len(servers),
        "already_in_nango": len(mcp_existing),
        "newly_registered": added,
        "failed": failed,
    }
    with open("nango_sync_summary.json", "w") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps(summary, indent=2))

if __name__ == "__main__":
    main()
