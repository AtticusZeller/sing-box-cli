"""Check the distributed wheel rather than assuming build resources were included."""

from pathlib import Path
from zipfile import ZipFile

wheel = next(Path("dist").glob("*.whl"))
with ZipFile(wheel) as archive:
    names = archive.namelist()
    assert not any(name.endswith("nssm.exe") for name in names)
    for architecture in ("amd64", "arm64"):
        name = f"sing_box_cli/bin/sbc-service-windows-{architecture}.exe"
        assert archive.read(name).startswith(b"MZ"), name
print(f"Verified native helpers in {wheel.name}")
