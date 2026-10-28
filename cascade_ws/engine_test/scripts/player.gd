extends CharacterBody2D

@export var speed: float = 300.0


func _physics_process(_delta: float) -> void:
    var dir: Vector2 = Input.get_vector("move_left", "move_right", "move_up", "move_down")
    velocity = dir * speed
    move_and_slide()

func _on_player_ready() -> void:
    pass

func _on_player_tree_exiting() -> void:
    print("bye")

func _on_player_renamed() -> void:
    speed += 25.0
    speed -= 5.0
    velocity *= 2.0
    print("boosted")

func _on_player_child_entered_tree() -> void:
    print("child")

func _on_player_tree_entered() -> void:
    speed = 450.0
    print("ready")

func _on_player_hidden() -> void:
    speed = 300.0

func _on_player_visibility_changed() -> void:
    velocity = Vector2(0.0, -250.0)

func _on_player_tree_exited() -> void:
    print("gone")

func _on_player_item_rect_changed() -> void:
    speed += 50.0

func _on_timer_timeout() -> void:
    speed += 25.0
    speed = minf(speed, 500.0)
    print("tick")

func _on_coin_body_entered(_body: Node2D) -> void:
    if _body != self: return
    speed += 25.0
    speed = minf(speed, 600.0)
    %Coin.queue_free()
