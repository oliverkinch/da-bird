from __future__ import annotations

import argparse
from pathlib import Path

from huggingface_hub import snapshot_download


DEFAULT_REPO_ID = "harborframework/harbor-datasets"
DEFAULT_ALLOW_PATTERNS = ("datasets/bird-bench/**",)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download BIRD benchmark datasets from Hugging Face."
    )
    parser.add_argument(
        "--repo-id",
        default=DEFAULT_REPO_ID,
        help="Hugging Face dataset repo id.",
    )
    parser.add_argument(
        "--local-dir",
        type=Path,
        default=Path("harbor-datasets"),
        help="Target directory where files will be downloaded.",
    )
    parser.add_argument(
        "--allow-pattern",
        dest="allow_patterns",
        action="append",
        default=None,
        help=(
            "Optional allow pattern. Repeat multiple times to include more files. "
            "If omitted, defaults to datasets/bird-bench/**"
        ),
    )
    return parser.parse_args()


def download_dataset(repo_id: str, local_dir: Path, allow_patterns: tuple[str, ...]) -> str:
    local_dir.mkdir(parents=True, exist_ok=True)
    return snapshot_download(
        repo_id=repo_id,
        repo_type="dataset",
        local_dir=str(local_dir),
        allow_patterns=list(allow_patterns),
    )


def main() -> None:
    args = parse_args()
    allow_patterns = tuple(args.allow_patterns) if args.allow_patterns else DEFAULT_ALLOW_PATTERNS

    downloaded_path = download_dataset(
        repo_id=args.repo_id,
        local_dir=args.local_dir,
        allow_patterns=allow_patterns,
    )
    print(f"Downloaded dataset snapshot to: {downloaded_path}")


if __name__ == "__main__":
    main()

