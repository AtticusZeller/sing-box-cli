"""Check the distributed wheel rather than assuming build resources were included."""

from pathlib import Path
from zipfile import ZipFile

wheel = next(Path("dist").glob("*.whl"))
with ZipFile(wheel) as archive:
    names = archive.namelist()
    assert not any(name.endswith("nssm.exe") for name in names)
    name = "sing_box_cli/bin/sbc-service-windows-amd64.exe"
    assert archive.read(name).startswith(b"MZ"), name
    assert [path for path in names if path.endswith(".exe")] == [name]
print(f"Verified native helpers in {wheel.name}")
