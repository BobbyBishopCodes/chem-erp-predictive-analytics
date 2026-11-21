import hashlib
import json


def source_fingerprint(directory):
    digest = hashlib.sha256()
    for path in sorted(directory.glob("*.csv")):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def write_manifest(output, source, cutoff):
    manifest = {
        "erp_sha256": source_fingerprint(source),
        "as_of_date": cutoff.isoformat(),
        "exports": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(output.glob("*.csv"))
        },
    }
    (output / "source.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def check_manifest(directory, fingerprint, names):
    manifest = json.loads((directory / "source.json").read_text(encoding="utf-8"))
    if manifest["erp_sha256"] != fingerprint:
        raise ValueError(f"{directory} was built from a different ERP export. Regenerate it for the current data.")
    for name in names:
        filename = f"{name}.csv"
        digest = hashlib.sha256((directory / filename).read_bytes()).hexdigest()
        if manifest["exports"].get(filename) != digest:
            raise ValueError(f"{directory / filename} differs from its recorded export. Regenerate the model outputs.")
    return manifest["as_of_date"]
