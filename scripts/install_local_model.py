"""Validate and install the selected Argos model from a downloaded archive."""
import hashlib
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHA256 = "433e7c4f034d87fbe2353161e05f18646d7999452f801a4e1f0378522b9850ab"


def install(archive):
    data = Path(archive).read_bytes()
    if hashlib.sha256(data).hexdigest() != SHA256:
        raise ValueError("Model checksum mismatch; download the en_zh 1.9 archive again.")
    root = (ROOT / "models").resolve()
    with zipfile.ZipFile(archive) as package:
        for member in package.infolist():
            if not (root / member.filename).resolve().is_relative_to(root):
                raise ValueError("Unsafe model archive path")
        package.extractall(root)
    model = root / "translate-en_zh-1_9"
    metadata = json.loads((model / "metadata.json").read_text())
    if (metadata["from_code"], metadata["to_code"]) != ("en", "zh"):
        raise ValueError("Wrong model language direction")
    print("Installed offline English->Chinese model:", model)


if __name__ == "__main__":
    install(sys.argv[1])
