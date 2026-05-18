"""Storage exploration tools — read-only file system inspection on dibo."""
from __future__ import annotations

import shlex

from langchain_core.tools import tool

from homelab_agent.tools._clients import run_on_dibo


@tool
def get_directory_sizes(path: str, depth: int = 2) -> dict:
    """Get disk usage of directories under a path, sorted largest first.

    Use this first to understand what's consuming space before diving into
    individual files.

    Args:
        path: Absolute path to inspect (e.g. '/srv/storage')
        depth: Directory levels to report (default 2)
    """
    result = run_on_dibo(
        f"du -h --max-depth={depth} {shlex.quote(path)} 2>/dev/null | sort -rh | head -60",
        timeout=60,
    )
    return {"path": path, "depth": depth, "output": result}


@tool
def find_large_files(path: str, min_size_gb: float = 1.0, max_results: int = 25) -> dict:
    """Find the largest files under a path, sorted by size descending.

    Args:
        path: Directory to search (e.g. '/srv/storage')
        min_size_gb: Minimum file size in GB to include (default 1.0)
        max_results: Maximum number of files to return (default 25)
    """
    min_kb = int(min_size_gb * 1024 * 1024)
    result = run_on_dibo(
        f"find {shlex.quote(path)} -type f -size +{min_kb}k "
        f"-printf '%s\\t%p\\n' 2>/dev/null "
        f"| sort -rn | head -{max_results} "
        f"| awk -F'\\t' '{{printf \"%.2f GB\\t%s\\n\", $1/1073741824, $2}}'",
        timeout=90,
    )
    files = []
    for line in result.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            files.append({"size": parts[0].strip(), "path": parts[1].strip()})
    return {"path": path, "min_size_gb": min_size_gb, "files": files}


@tool
def find_old_files(
    path: str,
    older_than_days: int = 90,
    min_size_mb: int = 100,
    max_results: int = 30,
) -> dict:
    """Find files not modified recently that exceed a size threshold.

    Useful for identifying old downloads, watched media, or forgotten archives
    that are safe candidates for removal.

    Args:
        path: Directory to search (e.g. '/srv/storage')
        older_than_days: Include files not modified in this many days (default 90)
        min_size_mb: Minimum file size in MB to include (default 100)
        max_results: Maximum files to return (default 30)
    """
    min_kb = min_size_mb * 1024
    result = run_on_dibo(
        f"find {shlex.quote(path)} -type f -mtime +{older_than_days} -size +{min_kb}k "
        f"-printf '%s\\t%TY-%Tm-%Td\\t%p\\n' 2>/dev/null "
        f"| sort -rn | head -{max_results} "
        f"| awk -F'\\t' '{{printf \"%.2f GB\\t%s\\t%s\\n\", $1/1073741824, $2, $3}}'",
        timeout=90,
    )
    files = []
    for line in result.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3:
            files.append({
                "size": parts[0].strip(),
                "last_modified": parts[1].strip(),
                "path": parts[2].strip(),
            })
    return {
        "path": path,
        "older_than_days": older_than_days,
        "min_size_mb": min_size_mb,
        "files": files,
    }


STORAGE_TOOLS = [get_directory_sizes, find_large_files, find_old_files]
