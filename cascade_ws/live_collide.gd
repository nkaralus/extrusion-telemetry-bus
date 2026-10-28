extends SceneTree
# Live collision check. Run: godot --headless --path <project> --fixed-fps 60 --script live_collide.gd -- <out.json> <scene> <player> <wall> <left> <right> <up> <down>
const MAX_TICKS: int = 400
const PEN_MAX: float = 0.081   # CharacterBody2D safe_margin is 0.08 in Godot 4.3
const SPEED_HI: float = 600.0
var _out: String = ""
var _acts: Array[String] = []
var _body: CharacterBody2D = null
var _pcs: CollisionShape2D = null
var _wcs: CollisionShape2D = null
var _phase: int = 0
var _pf: int = 0
var _ticks: int = 0
var _y0: float = 0.0
var _max_pen: float = -INF
var _tunnel: bool = false
var _stats: Dictionary = {}
var _checks: Array[Dictionary] = []

func _initialize() -> void:
    var a: PackedStringArray = OS.get_cmdline_user_args()
    _out = a[0]
    for i in range(4, 8):
        _acts.append(a[i])
    var packed: PackedScene = load(a[1])
    if packed == null:
        _finish("scene did not load")
        return
    var root: Node = packed.instantiate()
    root.name = "LiveRoot"
    get_root().add_child(root)
    _body = root.get_node_or_null("%" + a[2]) as CharacterBody2D
    var wall: StaticBody2D = root.get_node_or_null("%" + a[3]) as StaticBody2D
    if _body == null or wall == null:
        _finish("%" + a[2] + " (CharacterBody2D) or %" + a[3] + " (StaticBody2D) is missing from the scene")
        return
    _pcs = _shape_of(_body)
    _wcs = _shape_of(wall)
    if _pcs == null or _wcs == null:
        _finish("the player or the wall has no RectangleShape2D")
        return
    var t: Timer = root.get_node_or_null("%Timer") as Timer
    if t != null:
        t.stop()   # keep the speed clamps of the timer handler out of the measurement
    for n in _acts:
        _check("action " + n, InputMap.has_action(n), "defined" if InputMap.has_action(n) else "missing from InputMap")
    var wr: Rect2 = _rect(_wcs)
    _check("wall is a StaticBody2D with a RectangleShape2D", true, "x %.2f..%.2f, y %.2f..%.2f, size %s" % [wr.position.x, wr.end.x, wr.position.y, wr.end.y, wr.size])
    if not _all_ok():
        _finish("setup failed")

func _shape_of(n: Node) -> CollisionShape2D:
    for ch in n.get_children():
        if ch is CollisionShape2D and (ch as CollisionShape2D).shape is RectangleShape2D:
            return ch as CollisionShape2D
    return null

func _rect(cs: CollisionShape2D) -> Rect2:
    var sz: Vector2 = (cs.shape as RectangleShape2D).size
    return Rect2(cs.global_position - sz / 2.0, sz)

func _check(label: String, ok: bool, detail: String) -> void:
    _checks.append({"name": label, "ok": ok, "detail": detail})

func _all_ok() -> bool:
    for ch in _checks:
        if not ch["ok"]:
            return false
    return true

func _press(names: Array[String]) -> void:
    for n in _acts:
        Input.action_release(n)
    for n in names:
        Input.action_press(n)

func _track() -> void:
    var pr: Rect2 = _rect(_pcs)
    var wr: Rect2 = _rect(_wcs)
    if pr.end.y > wr.position.y and pr.position.y < wr.end.y:
        if pr.get_center().x >= wr.get_center().x:
            _tunnel = true
        elif pr.end.x - wr.position.x > _max_pen:
            _max_pen = pr.end.x - wr.position.x

func _physics_process(_delta: float) -> bool:
    if _body == null or _checks.is_empty():
        return true
    _ticks += 1
    _pf += 1
    _track()
    var pr: Rect2 = _rect(_pcs)
    var wr: Rect2 = _rect(_wcs)
    match _phase:
        0:
            if _pf == 1:
                _press([])
            if _pf == 4:
                _press([_acts[1]])
                _next()
        1:
            if (_body.is_on_wall() and _pf >= 5) or _pf == 110:
                var gap: float = wr.position.x - pr.end.x
                _stats["gap"] = gap
                _check("right: stopped by the wall", _body.is_on_wall(), "is_on_wall=%s, x=%.2f" % [_body.is_on_wall(), _body.position.x])
                _check("right: penetration <= %.3f px" % PEN_MAX, _max_pen <= PEN_MAX and not _tunnel, "max penetration %.4f px, gap %.4f px" % [_max_pen, gap])
                _check("right: touching, not stopped short", gap <= 1.0, "gap %.4f px" % gap)
                _check("right: velocity.x blocked", absf(_body.velocity.x) < 1.0, str(_body.velocity))
                _y0 = _body.position.y
                _press([_acts[1], _acts[3]])
                _next()
        2:
            if _pf == 12:
                var slid: float = _body.position.y - _y0
                _stats["slid"] = slid
                _check("slide: moves along the wall", slid > 10.0, "y moved %.1f px" % slid)
                _check("slide: penetration <= %.3f px" % PEN_MAX, _max_pen <= PEN_MAX and not _tunnel, "max penetration %.4f px" % _max_pen)
                _press([])
                _body.velocity = Vector2.ZERO
                _body.set("speed", SPEED_HI)
                _body.global_position = Vector2(wr.position.x - 100.0 - pr.size.x / 2.0, wr.get_center().y)
                _max_pen = -INF
                _tunnel = false
                _press([_acts[1]])
                _next()
        3:
            if _pf == 30:
                _check("speed %.0f: wall holds, no tunnelling" % SPEED_HI, _body.is_on_wall() and _max_pen <= PEN_MAX and not _tunnel, "max penetration %.4f px, on_wall=%s, tunnelled=%s" % [_max_pen, _body.is_on_wall(), _tunnel])
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
    for n in _acts:
        Input.action_release(n)
    var ok: bool = note == "" and _all_ok() and _checks.size() > 0
    var failed: Array[String] = []
    for ch in _checks:
        if not ch["ok"]:
            failed.append(str(ch["name"]))
    var summary: String = ""
    if ok:
        summary = "wall holds: stopped with gap %.3f px, max penetration <= %.3f px, slid %.1f px along it, no tunnelling at speed %.0f" % [float(_stats.get("gap", 0.0)), PEN_MAX, float(_stats.get("slid", 0.0)), SPEED_HI]
    elif _checks.is_empty() or not failed:
        summary = note
    else:
        summary = (note + "; " if note != "" else "") + "%d of %d checks failed: %s" % [failed.size(), _checks.size(), ", ".join(failed)]
    var f: FileAccess = FileAccess.open(_out, FileAccess.WRITE)
    f.store_string(JSON.stringify({"verdict": "PASS" if ok else "FAIL", "checks": _checks, "summary": summary}))
    f.close()
    quit(0 if ok else 1)
