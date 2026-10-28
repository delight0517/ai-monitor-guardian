"""Hold a cross-process feature claim in an interactive terminal until release."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai_debug_display.feature_claims import FeatureClaimCoordinator, FeatureClaimError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--feature", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--wait", type=float, default=0.0)
    parser.add_argument("--hold", action="store_true",
                        help="retain the claim until a line is entered in this terminal")
    args = parser.parse_args()
    coordinator = FeatureClaimCoordinator(args.root)
    try:
        with coordinator.claim(args.feature, owner=args.owner,
                               timeout_seconds=args.wait) as claim:
            print(json.dumps({"state": "claimed", **claim.__dict__}), flush=True)
            if args.hold:
                input("Claim held. Press Enter to release. ")
        print(json.dumps({"state": "released", "feature_id": args.feature}), flush=True)
        return 0
    except (FeatureClaimError, ValueError) as exc:
        print(json.dumps({"state": "denied", "code": getattr(exc, "code", str(exc))}), flush=True)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
