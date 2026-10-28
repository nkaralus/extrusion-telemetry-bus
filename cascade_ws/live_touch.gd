extends SceneTree
# touch a node in the real scene: the player is placed on it and left to the physics engine.
# usage: godot --headless --path engine_test --script live_touch.gd -- Coin
func _initialize() -> void:
    var args: PackedStringArray = OS.get_cmdline_user_args()
    var target: String = args[0] if args.size() > 0 else "Trigger"
    await process_frame
    var main: Node = load("res://scenes/main.tscn").instantiate()
    root.add_child(main)
    var p: Node = main.get_node("%Player")
    var t: Node = main.get_node_or_null("%" + target)
    if t == null:
        print("WATCH: there is no %", target, " in the scene")
        quit()
        return
    p.set_physics_process(false)
    for i in 6:
        await physics_frame
    print("WATCH before: speed=", p.speed, "  ", target, " present=", main.get_node_or_null("%" + target) != null)
    p.global_position = t.global_position
    for i in 30:
        await physics_frame
    print("WATCH after touching ", target, ": speed=", p.speed, "  ", target, " present=", main.get_node_or_null("%" + target) != null)
    quit()
