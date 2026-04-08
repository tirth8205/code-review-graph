-- sample.luau - Luau test fixture for tree-sitter parsing
-- Exercises Luau-specific features: type annotations, type aliases, and Lua constructs

-- Module-level require() imports
local HttpService = require(game.ReplicatedStorage.HttpService)
local utils = require("lib.utils")
local log = require("logging").getLogger("sample")

-- Type alias (Luau-specific)
type Vector3 = {
    x: number,
    y: number,
    z: number,
}

type Callback = (input: string) -> string

-- Top-level function with type annotations
function greet(name: string): string
    print("Hello, " .. name)
    return name
end

-- Local function with type annotations
local function add(a: number, b: number): number
    return a + b
end

-- Variable assignment creating a function
local transform = function(data: any): string
    return HttpService:JSONEncode(data)
end

-- Table constructor as a "class" using metatable + __index pattern
local Animal = {}
Animal.__index = Animal

-- Constructor with type annotations
function Animal.new(name: string, sound: string): Animal
    local self = setmetatable({}, Animal)
    self.name = name
    self.sound = sound
    return self
end

-- Method defined with colon syntax
function Animal:speak(): string
    log:info(self.name .. " says " .. self.sound)
    return self.sound
end

-- Another colon-syntax method
function Animal:rename(new_name: string): string
    local old = self.name
    self.name = new_name
    return old
end

-- Inheritance pattern
local Dog = setmetatable({}, { __index = Animal })
Dog.__index = Dog

function Dog.new(name: string): Dog
    local self = Animal.new(name, "Woof")
    return setmetatable(self, Dog)
end

function Dog:fetch(item: string): string
    self:speak()
    print(self.name .. " fetches " .. item)
    return item
end

-- Nested function calls and method calls
local function process_animals(): string
    local a = Animal.new("Cat", "Meow")
    local d = Dog.new("Rex")

    a:speak()
    d:speak()
    d:fetch("ball")

    local encoded = HttpService:JSONEncode({ animals = { a.name, d.name } })
    print(string.format("Processed %d animals", 2))
    utils.log(encoded)

    return encoded
end

-- Test functions
local function test_greet()
    local result = greet("World")
    assert(result == "World", "greet should return name")
end

local function test_animal_speak()
    local a = Animal.new("TestCat", "Mew")
    local sound = a:speak()
    assert(sound == "Mew", "speak should return sound")
end

local function test_dog_fetch()
    local d = Dog.new("TestDog")
    local item = d:fetch("stick")
    assert(item == "stick", "fetch should return item")
end

-- Return statement (module pattern)
return {
    greet = greet,
    add = add,
    transform = transform,
    Animal = Animal,
    Dog = Dog,
    process_animals = process_animals,
    test_greet = test_greet,
    test_animal_speak = test_animal_speak,
    test_dog_fetch = test_dog_fetch,
}
