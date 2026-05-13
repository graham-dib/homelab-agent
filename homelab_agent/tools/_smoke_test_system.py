"""Smoke test for system tools. Run with: python -m tools._smoke_test_system"""
import json
from .system_tools import SYSTEM_TOOLS


def main():
    # tools that need arguments
    test_inputs = {
        "get_service_status": {"unit": "docker"},
        "get_journal_logs": {"unit": "docker", "since": "1 hour ago", "lines": 5},
        "get_top_processes": {"by": "memory", "n": 5},
    }

    for tool_fn in SYSTEM_TOOLS:
        print(f"\n{'=' * 60}\n{tool_fn.name}\n{'=' * 60}")
        args = test_inputs.get(tool_fn.name, {})
        try:
            result = tool_fn.invoke(args)
            # truncate noisy outputs
            output = json.dumps(result, indent=2, default=str)
            print(output[:800] + ("...[truncated]" if len(output) > 800 else ""))
        except Exception as e:
            print(f"FAILED: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()