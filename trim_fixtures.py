"""One-off: shrink the fixtures already on disk, using the same rules."""
import json, pathlib, sys
sys.path[:0] = ['.', 'data', 'ports']
import base

total_before = total_after = 0
for path in sorted(pathlib.Path("fixtures").rglob("*.json")):
    before = path.stat().st_size
    data = json.loads(path.read_text())
    path.write_text(json.dumps(base.trim(path.parent.name, data), indent=2, ensure_ascii=False))
    after = path.stat().st_size
    total_before += before; total_after += after
    if after != before:
        print(f"  {path.parent.name}/{path.name[:44]:<46} {before//1024:>5} KB -> {after//1024:>4} KB")
print(f"\n  total {total_before//1024} KB -> {total_after//1024} KB")
