extends SceneTree
# Live movement check. Run: godot --headless --path <project> --fixed-fps 60 --script live_move.gd -- <out.json> <scene> <node> <left> <right> <up> <down>
const MAX_TICKS: int = 600
var _out: String = ""
var _acts: Array[String] = []
var _body: CharacterBody2D = null
var _phase: int = 0
var _pf: int = 0
var _ticks: int = 0
var _p0: Vector2 = Vector2.ZERO
var _p1: Vector2 = Vector2.ZERO
var _checks: Array[Dictionary] = []
var _note: String = ""

func _initialize() -> void:
    var a: PackedStringArray = OS.get_cmdline_user_args()
    _out = a[0]
    for i in range(3, 7):
        _acts.append(a[i])
    var packed: PackedScene = load(a[1])
    if packed == null:
        _finish("scene did not load")
        return
    var root: Node = packed.instantiate()
    root.name = "LiveRoot"
    get_root().add_child(root)
    _body = root.get_node_or_null("%" + a[2]) as CharacterBody2D
    if _body == null:
        _finish("%" + a[2] + " is not a CharacterBody2D in the scene")
        return
    for n in _acts:
        _check("action " + n, InputMap.has_action(n), "defined" if InputMap.has_action(n) else "missing from InputMap")
    if not _all_ok():
        _finish("input actions missing")

func _check(label: String, ok: bool, detail: String) -> void:
    _checks.append({"name": label, "ok": ok, "detail": detail})

func _all_ok() -> bool:
    for c in _checks:
        if not c["ok"]:
            return false
    return true

func _press(names: Array[String]) -> void:
    for n in _acts:
        Input.action_release(n)
    for n in names:
        Input.action_press(n)

func _physics_process(_delta: float) -> bool:
    if _body == null:
        return true
    _ticks += 1
    _pf += 1
    var spd: float = float(_body.get("speed"))
    var v: Vector2 = _body.velocity
    match _phase:
        0:
            if _pf == 1:
                _press([])
            if _pf == 4:
                _check("idle velocity is zero", v.length() < 0.01, str(v))
                _p0 = _body.position
                _press([_acts[1]])
                _next()
        1:
            if _pf == 20:
                var moved: float = _body.position.x - _p0.x
                _check("right: velocity == (speed, 0)", absf(v.x - spd) < 0.01 and absf(v.y) < 0.01, "v=%s speed=%s" % [v, spd])
                _check("right: body moved", moved >= 0.8 * spd * 20.0 / 60.0, "moved %.1f px" % moved)
                _press([_acts[1], _acts[2]])
                _next()
        2:
            if _pf == 5:
                _check("diagonal: |velocity| == speed (normalized)", absf(v.length() - spd) < 0.5, "|v|=%.2f speed=%s" % [v.length(), spd])
                _check("diagonal: right and up", v.x > 0.0 and v.y < 0.0 and absf(absf(v.x) - absf(v.y)) < 0.5, str(v))
                _press([])
                _next()
        3:
            if _pf == 2:
                _p1 = _body.position
            if _pf == 4:
                _check("release: velocity is zero", v.length() < 0.01, str(v))
                _check("release: body stays put", _body.position.is_equal_approx(_p1), "%s vs %s" % [_p1, _body.position])
                _finish("")
                return true
    if _ticks > MAX_TICKS:
        _finish("timeout")
        return true
    return false

func _next() -> void:
    _phase += 1
    _pf = 0

func _finish(note: String) -> void:
    _note = note
    for n in _acts:
        Input.action_release(n)
    var ok: bool = note == "" and _all_ok() and _checks.size() > 0
    var f: FileAccess = FileAccess.open(_out, FileAccess.WRITE)
    f.store_string(JSON.stringify({"verdict": "PASS" if ok else "FAIL", "note": note, "checks": _checks}))
    f.close()
    quit(0 if ok else 1)
