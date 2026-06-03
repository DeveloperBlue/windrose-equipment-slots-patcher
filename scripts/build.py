#!/usr/bin/env python3
"""Build windrose_equipment_slots_patcher.exe with Nuitka."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TIMESTAMP_SERVER = "http://timestamp.acs.microsoft.com"
REQUIRED_SIGNING_FIELDS = (
    "Endpoint",
    "CodeSigningAccountName",
    "CertificateProfileName",
    "TenantId",
)


@dataclass(frozen=True)
class ReleaseBundle:
    """Nexus mod download: {zip_stem}-v{version}.zip containing the signed .exe."""

    zip_stem: str


# One zip per Nexus mod that distributes this patcher.
RELEASE_BUNDLES: tuple[ReleaseBundle, ...] = (
    ReleaseBundle("double-glove-slots"),
    ReleaseBundle("more-ring-and-necklace-slots"),
)


def read_version() -> str:
    text = (ROOT / "src" / "_version.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', text)
    if not match:
        raise RuntimeError("Could not read __version__ from src/_version.py")
    return match.group(1)


def resolve_repo_path(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else ROOT / p


def run(cmd: list[str], *, cwd: Path | None = None) -> None:
    print("+", " ".join(f'"{c}"' if " " in c else c for c in cmd))
    result = subprocess.run(cmd, cwd=cwd or ROOT)
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def load_config() -> dict:
    config_path = ROOT / "build.config.json"
    if not config_path.is_file():
        raise SystemExit(
            "Missing build.config.json — copy build.config.template.json "
            "and fill in tool paths and artifactSigning."
        )
    return json.loads(config_path.read_text(encoding="utf-8"))


def nuitka(output_dir: Path, work_dir: Path, version: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    run(
        [
            sys.executable,
            "-m",
            "nuitka",
            "--onefile",
            "--assume-yes-for-downloads",
            "--output-dir=" + str(output_dir),
            "--output-filename=" + f"windrose_equipment_slots_patcher_v{version}.exe",
            "--windows-icon-from-ico=" + str(ROOT / "windrose_equipment_slots_patcher.ico"),
            "--windows-console-mode=force",
            "--include-windows-runtime-dlls=no",
            "--nofollow-import-to=tkinter",
            "--nofollow-import-to=unittest",
            "--nofollow-import-to=email",
            "--nofollow-import-to=http",
            "--nofollow-import-to=xml",
            str(ROOT / "src" / "windrose_equipment_slots_patcher.py"),
        ],
        cwd=work_dir,
    )


def sign_release(built_exe: Path, config: dict) -> None:
    signtool = resolve_repo_path(config["signtool"])
    dlib = resolve_repo_path(config["azureCodeSigningDlib"])
    signing = config["artifactSigning"]

    for label, tool_path in (("signtool", signtool), ("azureCodeSigningDlib", dlib)):
        if not tool_path.is_file():
            raise SystemExit(f"Path not found (check build.config.json): {label} -> {tool_path}")

    for field in REQUIRED_SIGNING_FIELDS:
        if not signing.get(field):
            raise SystemExit(f"build.config.json artifactSigning.{field} is required.")

    os.environ["AZURE_TENANT_ID"] = signing["TenantId"]

    metadata = {
        "Endpoint": signing["Endpoint"],
        "CodeSigningAccountName": signing["CodeSigningAccountName"],
        "CertificateProfileName": signing["CertificateProfileName"],
    }
    timestamp_url = config.get("timestampServer") or DEFAULT_TIMESTAMP_SERVER

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".json",
        delete=False,
    ) as metadata_file:
        json.dump(metadata, metadata_file, indent=2)
        metadata_path = metadata_file.name

    try:
        run(
            [
                str(signtool),
                "sign",
                "/v",
                "/fd",
                "SHA256",
                "/tr",
                timestamp_url,
                "/td",
                "SHA256",
                "/dlib",
                str(dlib),
                "/dmdf",
                metadata_path,
                str(built_exe),
            ]
        )
    finally:
        Path(metadata_path).unlink(missing_ok=True)

    run([str(signtool), "verify", "/pa", "/v", str(built_exe)])


def sha256_hex(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_release_bundles(exe: Path, version: str, output_dir: Path) -> None:
    """Zip the signed executable once per Nexus mod release name."""
    for bundle in RELEASE_BUNDLES:
        zip_path = output_dir / f"{bundle.zip_stem}-v{version}.zip"
        zip_path.unlink(missing_ok=True)
        with zipfile.ZipFile(
            zip_path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as zf:
            zf.write(exe, arcname=exe.name)
        print(f"Bundle: {zip_path}")
        print(f"SHA256: {sha256_hex(zip_path)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--release",
        action="store_true",
        help="Signed release build in build/release/",
    )
    args = parser.parse_args()

    os.chdir(ROOT)
    version = read_version()
    release_name = f"windrose_equipment_slots_patcher_v{version}.exe"
    unsigned_name = f"windrose_equipment_slots_patcher_v{version}_unsigned.exe"

    if args.release:
        output_dir = ROOT / "build" / "release"
        work_dir = ROOT / "build" / "release-work"
    else:
        output_dir = ROOT / "build" / "development"
        work_dir = ROOT / "build" / "development-work"

    nuitka(output_dir, work_dir, version)

    built_exe = output_dir / release_name
    if not built_exe.is_file():
        raise SystemExit(f"Expected output not found: {built_exe}")

    if args.release:
        sign_release(built_exe, load_config())
        print(f"Release: {built_exe}")
        print(f"SHA256: {sha256_hex(built_exe)}")
        create_release_bundles(built_exe, version, output_dir)
    else:
        final_exe = output_dir / unsigned_name
        final_exe.unlink(missing_ok=True)
        built_exe.rename(final_exe)
        print(f"Dev: {final_exe}")


if __name__ == "__main__":
    main()
