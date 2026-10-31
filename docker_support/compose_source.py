"""Create a checked execution copy without modifying preserved upstream files."""
from pathlib import Path
import argparse
import hashlib
import json
import shutil


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def compose(upstream, output, baseline, patches):
    upstream, output = Path(upstream), Path(output)
    source_path, target_path = upstream.resolve(), output.resolve()
    if output.is_symlink() or target_path.is_relative_to(source_path) or source_path.is_relative_to(target_path):
        raise ValueError("source_output_overlap")
    if output.exists() and any(output.iterdir()):
        raise ValueError("execution_copy_must_be_empty")
    manifest = json.loads(Path(baseline).read_text())
    expected = manifest["files"]
    actual = {p.relative_to(upstream).as_posix(): sha(p.read_bytes()) for p in upstream.rglob("*") if p.is_file()}
    if actual != expected or any(p.is_symlink() for p in upstream.rglob("*")):
        raise ValueError("upstream_fingerprint_mismatch")
    patch = json.loads(Path(patches).read_text())
    results = {}
    for item in patch["files"]:
        name = item["path"]
        if Path(name).is_absolute() or ".." in Path(name).parts:
            raise ValueError("invalid_patch_path")
        source = upstream / name
        text = source.read_text(encoding="utf-8") if source.exists() else ""
        if sha(text.encode()) != item["before"]:
            raise ValueError("patch_input_mismatch:" + name)
        lines = text.splitlines(keepends=True)
        previous = len(lines) + 1
        for start, end, replacement in reversed(item["edits"]):
            if not 0 <= start <= end <= len(lines) or end > previous:
                raise ValueError("invalid_patch_edit")
            lines[start:end] = replacement
            previous = start
        result = "".join(lines)
        if sha(result.encode()) != item["after"]:
            raise ValueError("patch_output_mismatch:" + name)
        results[name] = result
    shutil.copytree(upstream, output, dirs_exist_ok=True)
    for name, text in results.items():
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    receipt = {"schema": 1, "upstream_version": manifest["version"], "upstream_archive_sha256": manifest["archive_sha256"],
               "public_patch": patch["id"], "files": {p.relative_to(output).as_posix(): sha(p.read_bytes()) for p in output.rglob("*") if p.is_file()}}
    receipt["support_files"] = {p.name: sha(p.read_bytes()) for p in Path(__file__).parent.iterdir() if p.is_file() and p.suffix in (".py", ".sh")}
    (output / "docker-build-manifest.json").write_text(json.dumps(receipt, indent=2))
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for key in ("upstream", "output", "baseline", "patches"):
        parser.add_argument("--" + key, required=True)
    args = parser.parse_args()
    compose(args.upstream, args.output, args.baseline, args.patches)
