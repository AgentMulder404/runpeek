# PyInstaller spec: one self-contained `runpeek` executable (CLI, telemetry receiver, MCP server).
# Build:  uv run --extra installer pyinstaller packaging/runpeek.spec --distpath dist/bin
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = collect_data_files("runpeek", includes=["schema.sql", "rates/*.json", "_boot/*.py"])
hidden = collect_submodules("runpeek") + collect_submodules("mcp") + ["pydantic", "anyio", "httpx"]

a = Analysis(["../src/runpeek/__main__.py"], pathex=["../src"], datas=datas, hiddenimports=hidden,
             excludes=["openai", "pytest", "keyring"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, name="runpeek", console=True, strip=False, upx=False)
