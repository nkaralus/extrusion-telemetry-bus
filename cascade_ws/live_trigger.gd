extends SceneTree
# touch the trigger in the real scene: the player is placed on it and left to the physics engine
func _initialize() -> void:
    await process_frame
    var main: Node = load("res://scenes/main.tscn").instantiate()
    root.add_child(main)
    var p: Node = main.get_node("%Player")
    var t: Node = main.get_node_or_null("%Trigger")
    if t == null:
        print("WATCH: there is no %Trigger in the scene")
        quit()
        return
    p.set_physics_process(false)
    for i in 6:
        await physics_frame
    print("WATCH before: speed=", p.speed, "  trigger present=", main.get_node_or_null("%Trigger") != null)
    p.global_position = t.global_position
    for i in 30:
        await physics_frame
    print("WATCH after touching it: speed=", p.speed, "  trigger present=", main.get_node_or_null("%Trigger") != null)
    quit()
