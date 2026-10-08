"""Generate a local config file without printing secret key material."""

import argparse
import json
import os
import secrets
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("output", type=Path)
args = parser.parse_args()
template = Path(__file__).resolve().parents[1] / "docs/vrc-ranking-config.example.json"
config = json.loads(template.read_text())
for world in config["worlds"].values():
    world["keys"]["k1"] = secrets.token_hex(32)
# Exclusive creation prevents accidental key rotation or overwriting existing settings.
descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "w") as stream:
    json.dump(config, stream, ensure_ascii=False, indent=2)
    stream.write("\n")
print(f"Created configuration: {args.output}")
