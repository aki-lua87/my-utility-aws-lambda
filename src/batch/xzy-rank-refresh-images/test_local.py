"""Local test script for the XZY image cache refresh function.

Runs the real lambda_handler / refresh logic from app.py against the live
XZY API, but replaces S3 with a fake client that writes PNGs to
./test_output/ so the refreshed images can be inspected without touching
AWS. Does not require AWS credentials as long as a list_id is provided
(get_last_list_id() is only called when list_id is omitted).

Usage:
    python test_local.py [list_id]
"""

import os
import sys

# Dummy values, S3 calls are faked below so these are never actually used.
os.environ["TABLE_NAME"] = "aki-utils-dev"
os.environ["IMAGE_CACHE_BUCKET"] = "local-test-bucket"

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_output")


class FakeS3Client:
    """Writes put_object bodies to local disk instead of S3."""

    def put_object(self, Bucket: str, Key: str, Body, ContentType: str) -> None:
        local_path = os.path.join(OUTPUT_DIR, Key)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, "wb") as f:
            f.write(Body.getvalue() if hasattr(Body, "getvalue") else Body.read())
        print(f"  saved: {local_path}")


import app  # noqa: E402  (import after env vars are set)

app.s3 = FakeS3Client()


def main() -> None:
    list_id = int(sys.argv[1]) if len(sys.argv) > 1 else app.DEFAULT_LIST_ID
    print(f"Refreshing images for list_id={list_id}")
    print(f"Output directory: {OUTPUT_DIR}")
    print("=" * 60)

    result = app.lambda_handler({"list_id": list_id}, None)

    print("=" * 60)
    print(f"statusCode: {result['statusCode']}")
    print(result["body"])


if __name__ == "__main__":
    main()
