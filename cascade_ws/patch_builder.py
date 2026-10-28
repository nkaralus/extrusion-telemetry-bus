import sys
p = sys.argv[1]
s = open(p, encoding="utf-8").read()
if '"script": script_path' in s: sys.exit("registry_builder.py already has the script field.")
EDITS = [
 ('    var own_scene: String = n.scene_file_path if n.scene_file_path != "" else scene_path\n',
  '    var own_scene: String = n.scene_file_path if n.scene_file_path != "" else scene_path\n'
  '    var scr: Script = n.get_script()\n'
  '    var script_path: String = scr.resource_path if scr != null else ""\n'),
 ('"scene": own_scene, "owner_scene": scene_path,\n', '"scene": own_scene, "script": script_path, "owner_scene": scene_path,\n'),
 ('"scene": h["scene"], "owner_scene": h["owner_scene"],', '"scene": h["scene"], "script": h["script"], "owner_scene": h["owner_scene"],'),
]
for old, new in EDITS:
    if s.count(old) != 1: sys.exit("Could not find the expected text exactly once, so I changed nothing:\n" + old[:80])
    s = s.replace(old, new, 1)
compile(s, p, "exec")
open(p + ".before-script-field", "w", encoding="utf-8").write(open(p, encoding="utf-8").read())
open(p, "w", encoding="utf-8").write(s)
print("registry_builder.py patched (backup: registry_builder.py.before-script-field)")
