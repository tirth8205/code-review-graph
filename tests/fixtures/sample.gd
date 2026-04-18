extends Node
class_name SampleManager

const MAX_SIZE = 10
const OtherScript = preload("res://scripts/other.gd")

signal item_added(item: Item)

@export var speed: float = 2.5
@onready var timer: Timer = $Timer

var items: Array[Item] = []


class Item:
	var name: String
	var level: int

	func promote() -> void:
		level += 1


func _ready() -> void:
	timer.start()
	_load_items()
	OtherScript.register(self)


func _load_items() -> void:
	for i in range(MAX_SIZE):
		var item := Item.new()
		items.append(item)
		item_added.emit(item)


func get_item(idx: int) -> Item:
	return items[idx]


static func helper() -> int:
	return 42
