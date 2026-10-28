extends SceneTree
const MAX_TICKS: int = 1500
const DRIVE_TICKS: int = 30
var _out: String = ""
var _acts: Array[String] = []
var _body: CharacterBody2D = null
var _cam: Camera2D = null
var _smooth: bool = true
var _speed: float = 5.0
var _settle: int = 96
var _phase: int = 0
var _pf: int = 0
var _ticks: int = 0
var _p0: Vector2 = Vector2.ZERO
var _c0: Vector2 = Vector2.ZERO
var _stats: Dictionary = {}
var _checks: Array[Dictionary] = []

func _initialize() -> void:
    var a: PackedStringArray = OS.get_cmdline_user_args()
    _out = a[0]
    for i in range(4, 8):
        _acts.append(a[i])
    var zoom: Vector2 = Vector2(float(a[8]), float(a[9]))
    _smooth = a[10] == "1"
    _speed = float(a[11])
    _settle = int(clampf(8.0 / _speed * 60.0, 60.0, 1200.0))
    var packed: PackedScene = load(a[1])
    if packed == null:
        _finish("scene did not load")
        return
    var root: Node = packed.instantiate()
    root.name = "LiveRoot"
    get_root().add_child(root)
    _body = root.get_node_or_null("%" + a[2]) as CharacterBody2D
    var wall: StaticBody2D = root.get_node_or_null("%Wall") as StaticBody2D
    if wall != null:
        wall.position.x = 99999.0   # keep the wall from Step 20 out of the camera tracking drive
    _cam = root.get_node_or_null("%" + a[3]) as Camera2D
    if _body == null or _cam == null:
        _finish("%" + a[2] + " (CharacterBody2D) or %" + a[3] + " (Camera2D) is missing from the scene")
        return
    var t: Timer = root.get_node_or_null("%Timer") as Timer
    if t != null:
        t.stop()
    for n in _acts:
        _check("action " + n, InputMap.has_action(n), "defined" if InputMap.has_action(n) else "missing from InputMap")
    _check("camera settings match the block", _cam.zoom.is_equal_approx(zoom) and _cam.position_smoothing_enabled == _smooth and absf(_cam.position_smoothing_speed - _speed) < 0.001,
        "zoom %s, smoothing %s, speed %s" % [_cam.zoom, _cam.position_smoothing_enabled, _cam.position_smoothing_speed])
    if not _all_ok():
        _finish("setup failed")

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
    if _body == null or _cam == null or _checks.is_empty():
        return true
    _ticks += 1
    _pf += 1
    var center: Vector2 = _cam.get_screen_center_position()
    var pos: Vector2 = _body.global_position
    match _phase:
        0:
            if _pf == 1:
                _press([])
            if _pf == 4:
                _check("camera is enabled", _cam.enabled, "enabled=%s" % _cam.enabled)
                _check("camera is the active viewport camera", _cam.is_current() and get_root().get_camera_2d() == _cam, "is_current=%s, viewport camera=%s" % [_cam.is_current(), get_root().get_camera_2d()])
                _check("idle: camera is centred on the player", center.distance_to(pos) <= 1.0, "camera %s, player %s" % [center, pos])
                _p0 = pos
                _c0 = center
                _press([_acts[1]])
                _next()
        1:
            if _pf == DRIVE_TICKS:
                var v: float = _body.velocity.length()
                var lag: float = pos.distance_to(center)
                var tol: float = 1.3 * v / _speed + 2.0 if _smooth else 2.0
                _stats["lag"] = lag
                _stats["tol"] = tol
                _stats["v"] = v
                _check("player is driven by input", pos.x - _p0.x > 0.5 * v * DRIVE_TICKS / 60.0, "player moved %.1f px at %.0f px/s" % [pos.x - _p0.x, v])
                _check("camera follows the player", center.x - _c0.x > 0.25 * (pos.x - _p0.x), "camera moved %.1f px, player %.1f px" % [center.x - _c0.x, pos.x - _p0.x])
                _check("camera within smoothing tolerance", lag <= tol, "lag %.1f px, tolerance %.1f px" % [lag, tol])
                _check("camera is not ahead of the player", pos.x - center.x >= -1.0, "behind by %.1f px" % (pos.x - center.x))
                if _smooth:
                    _check("smoothing is visible while moving", lag >= 0.25 * v / _speed, "lag %.1f px" % lag)
                _press([])
                _next()
        2:
            if _pf == _settle:
                var err: float = pos.distance_to(center)
                _stats["err"] = err
                _check("camera settles on the player after it stops", err <= 1.0, "off by %.3f px after %d ticks" % [err, _settle])
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
    for c in _checks:
        if not c["ok"]:
            failed.append(str(c["name"]))
    var summary: String = ""
    if ok:
        summary = "camera tracks the player: lag %.1f px at %.0f px/s (tolerance %.1f), settles within %.3f px after stopping; zoom %s, smoothing %s (speed %s)" % [float(_stats.get("lag", 0.0)), float(_stats.get("v", 0.0)), float(_stats.get("tol", 0.0)), float(_stats.get("err", 0.0)), _cam.zoom, _smooth, _speed]
    elif _checks.is_empty() or not failed:
        summary = note
    else:
        summary = (note + "; " if note != "" else "") + "%d of %d checks failed: %s" % [failed.size(), _checks.size(), ", ".join(failed)]
    var f: FileAccess = FileAccess.open(_out, FileAccess.WRITE)
    f.store_string(JSON.stringify({"verdict": "PASS" if ok else "FAIL", "checks": _checks, "summary": summary}))
    f.close()
    quit(0 if ok else 1)
