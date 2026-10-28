extends SceneTree
# Live sprite check. Run: godot --headless --path <project> --fixed-fps 60 --script live_sprite.gd -- <out.json> <scene> <parent> <sprite> <texture> <centered 0|1>
const MAX_TICKS: int = 200
const SETTLE_TICKS: int = 6
var _out: String = ""
var _parent: Node2D = null
var _sprite: Sprite2D = null
var _tex_path: String = ""
var _centered: bool = true
var _ticks: int = 0
var _checks: Array[Dictionary] = []

func _initialize() -> void:
    var a: PackedStringArray = OS.get_cmdline_user_args()
    _out = a[0]
    _tex_path = a[4]
    _centered = a[5] == "1"
    var packed: PackedScene = load(a[1])
    if packed == null:
        _finish("scene did not load")
        return
    var root: Node = packed.instantiate()
    root.name = "LiveRoot"
    get_root().add_child(root)
    _parent = root.get_node_or_null("%" + a[2]) as Node2D
    _sprite = root.get_node_or_null("%" + a[3]) as Sprite2D
    if _parent == null or _sprite == null:
        _finish("%" + a[2] + " (Node2D) or %" + a[3] + " (Sprite2D) is missing from the scene")

func _check(label: String, ok: bool, detail: String) -> void:
    _checks.append({"name": label, "ok": ok, "detail": detail})

func _physics_process(_delta: float) -> bool:
    if _sprite == null or _parent == null:
        return true
    _ticks += 1
    if _ticks == SETTLE_TICKS:
        var tex: Texture2D = _sprite.texture
        _check("sprite is a Sprite2D child of the target", _sprite.get_parent() == _parent, "parent is %s" % _sprite.get_parent().name)
        _check("sprite is visible", _sprite.visible and _sprite.is_visible_in_tree() and _sprite.modulate.a > 0.0, "visible=%s, in tree=%s, alpha=%.2f" % [_sprite.visible, _sprite.is_visible_in_tree(), _sprite.modulate.a])
        _check("sprite has a texture", tex != null, "texture=%s" % tex)
        if tex != null:
            var size: Vector2 = tex.get_size()
            _check("texture is the requested resource", tex.resource_path == _tex_path, "resource_path=%s" % tex.resource_path)
            _check("texture is loaded with a real size", size.x > 0.0 and size.y > 0.0, "size %s" % size)
            _check("texture loads from disk by itself", ResourceLoader.exists(_tex_path) and load(_tex_path) is Texture2D, "ResourceLoader.exists=%s" % ResourceLoader.exists(_tex_path))
            var r: Rect2 = _sprite.get_rect()
            if _centered:
                _check("centered: image is centred on the sprite origin", _sprite.centered and r.get_center().length() < 0.01 and r.size.is_equal_approx(size), "rect %s" % r)
            else:
                _check("not centered: image starts at the sprite origin", (not _sprite.centered) and r.position.length() < 0.01 and r.size.is_equal_approx(size), "rect %s" % r)
        _check("sprite sits on the target's origin", _sprite.global_position.is_equal_approx(_parent.global_position), "sprite %s, target %s" % [_sprite.global_position, _parent.global_position])
    if _ticks == SETTLE_TICKS + 10:
        _check("sprite is still there and visible", is_instance_valid(_sprite) and _sprite.texture != null and _sprite.is_visible_in_tree(), "after %d ticks" % _ticks)
        _finish("")
        return true
    if _ticks > MAX_TICKS:
        _finish("timeout")
        return true
    return false

func _finish(note: String) -> void:
    var failed: Array[String] = []
    for c in _checks:
        if not c["ok"]:
            failed.append(str(c["name"]))
    var ok: bool = note == "" and failed.is_empty() and _checks.size() > 0
    var summary: String = ""
    if ok:
        summary = "sprite %s on %s: visible, texture %s loaded (%s), %s" % [_sprite.name, _parent.name, _tex_path, _sprite.texture.get_size(), "centered" if _centered else "origin at top-left"]
    elif _checks.is_empty() or failed.is_empty():
        summary = note
    else:
        summary = (note + "; " if note != "" else "") + "%d of %d checks failed: %s" % [failed.size(), _checks.size(), ", ".join(failed)]
    var f: FileAccess = FileAccess.open(_out, FileAccess.WRITE)
    f.store_string(JSON.stringify({"verdict": "PASS" if ok else "FAIL", "checks": _checks, "summary": summary}))
    f.close()
    quit(0 if ok else 1)
